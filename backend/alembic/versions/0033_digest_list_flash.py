"""周报期刊加名单速报和本期清单（公众号方案 PR #287 第 3 件事）。

- subject_id：名单速报一份名单一篇，记那份名单的文件编号；周报和临期提醒为 0，唯一约束照旧
  是同一方案每周每种稿件一份。
- items：周报这一期的全部条目（小程序"本期清单"用；正文每省只列 5 条）。旧期刊为空。
- scope：生成时的范围（"全国"或方案名称）；"我的 → 本周周报"按它找全国版。已有期刊按所属方案
  现在的设置回填（不限省份、不筛关键词的写"全国"；限了范围却叫"全国"的写"全国·限定范围"，
  和 weekly.scope 一样），升级前发布的全国版周报也找得到。旧期刊没记当时的范围，方案发布后
  又改过范围的只能按现在的算（这次升级前公众号还没开，没有真正发布过的周报）。

编号 0033 是并行开发时预留的（0034 #297 也还没合并）；main 上现在是
0032 → 0037 → 0038 → 0036 → 0039 → 0035 → 0040 → 0041 → 0042 → 0043（10/28），本迁移接在 0043 后面，
避免出现两个 head。
谁后合并谁把 down_revision 接到当时 main 最新的那个。

Revision ID: 0033
Revises: 0043
"""

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "digest_issues",
        sa.Column("subject_id", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("digest_issues", sa.Column("items", sa.JSON(), nullable=True))
    op.add_column(
        "digest_issues", sa.Column("scope", sa.String(80), server_default="", nullable=False)
    )
    op.execute(
        "UPDATE digest_issues di JOIN digest_profiles dp ON dp.id = di.profile_id "
        "SET di.scope = IF(JSON_LENGTH(dp.provinces) = 0 AND JSON_LENGTH(dp.keywords) = 0, '全国', "
        "IF(TRIM(dp.name) = '全国', '全国·限定范围', LEFT(dp.name, 80)))"
    )
    # 先建以 profile_id 开头的新 unique 约束，方案外键始终有可用索引，再删旧约束（同 0016）。
    op.create_unique_constraint(
        "uq_digest_issue_subject",
        "digest_issues",
        ["profile_id", "kind", "week_start", "subject_id"],
    )
    op.drop_constraint("uq_digest_issue_kind_week", "digest_issues", type_="unique")
    op.drop_constraint("ck_digest_issue_kind", "digest_issues", type_="check")
    op.create_check_constraint(
        "ck_digest_issue_kind", "digest_issues", "kind IN ('weekly', 'reminder', 'list_flash')"
    )


def downgrade() -> None:
    # 回退前删掉名单速报，保留周报和临期提醒（它们的 subject_id 都是 0，旧约束不会冲突）。
    op.execute("DELETE FROM digest_issues WHERE kind = 'list_flash'")
    op.drop_constraint("ck_digest_issue_kind", "digest_issues", type_="check")
    op.create_check_constraint(
        "ck_digest_issue_kind", "digest_issues", "kind IN ('weekly', 'reminder')"
    )
    op.create_unique_constraint(
        "uq_digest_issue_kind_week", "digest_issues", ["profile_id", "kind", "week_start"]
    )
    op.drop_constraint("uq_digest_issue_subject", "digest_issues", type_="unique")
    op.drop_column("digest_issues", "scope")
    op.drop_column("digest_issues", "items")
    op.drop_column("digest_issues", "subject_id")
