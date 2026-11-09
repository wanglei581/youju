"""覆盖看板（全国数据覆盖第二阶段，D-122）：四张新表，只新增、不动已有表。

- coverage_divisions：行政区（国家地名信息库 31 省、333 地级、2847 县级，加国家级 000000）和盘点找到的门户；
- coverage_cells：一格 = 行政区 × 目标部门（开发区可有几个，按 seq 区分），存盘点结论、找到的栏目、
  看板状态（已收录 / 抓取失败 / 网站无此栏目 / 需人工 / 不能抓 / 待启用）和人工确认；
- coverage_cell_sources：格 ↔ 来源（多对多），来源只停用不删除（RESTRICT）；
- coverage_acceptances：按省抽查记录（D-122：每省抽查合格才算接好）。

写这几张表的只有 admin 服务（youju coverage-import / coverage-refresh、后台覆盖看板页），
admin 账号已有全部权限；api 账号已有全库 SELECT，不用新授权。

编号：0048 由协调会话 11/8 给定（main 上最新是 0046；0047 留给问一问第二批，谁后合进 main 谁改 down_revision）。

Revision ID: 0048
Revises: 0046
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0048"
down_revision = "0046"
branch_labels = None
depends_on = None
OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}
NOW = sa.text("CURRENT_TIMESTAMP(6)")


def upgrade() -> None:
    op.create_table(
        "coverage_divisions",
        sa.Column("code", sa.String(6), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("level", sa.String(8), nullable=False),
        sa.Column("province", sa.String(32), nullable=False),
        sa.Column("city", sa.String(32), nullable=True),
        sa.Column("district", sa.String(32), nullable=True),
        sa.Column("parent_code", sa.String(6), nullable=True),
        sa.Column("portal_status", sa.String(8), nullable=False),
        sa.Column("portal_url", sa.String(512), nullable=True),
        sa.Column("portal_reason", sa.String(300), nullable=True),
        sa.Column("portal_nav_source", sa.String(512), nullable=True),
        sa.Column("site_id_code", sa.String(20), nullable=True),
        sa.Column("platform", sa.String(64), nullable=True),
        sa.Column("inventoried_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.PrimaryKeyConstraint("code"),
        **OPTIONS,
    )
    op.create_index("ix_coverage_divisions_level", "coverage_divisions", ["level"])
    op.create_index("ix_coverage_divisions_province", "coverage_divisions", ["province"])

    op.create_table(
        "coverage_cells",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("division_code", sa.String(6), nullable=False),
        sa.Column("dept", sa.String(16), nullable=False),
        sa.Column("seq", sa.Integer(), server_default="0", nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("dept_full_name", sa.String(128), nullable=True),
        sa.Column("site_url", sa.String(512), nullable=True),
        sa.Column("site_type", sa.String(8), nullable=True),
        sa.Column("nav_source", sa.String(512), nullable=True),
        sa.Column("inventory_status", sa.String(8), nullable=False),
        sa.Column("inventory_reason", sa.String(300), nullable=True),
        sa.Column("columns", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(8), nullable=False),
        sa.Column("reason", sa.String(300), nullable=True),
        sa.Column("last_success_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("state_changed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("refreshed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("confirmed_no_column", sa.Boolean(), nullable=False),
        sa.Column("confirmed_by", sa.String(80), nullable=True),
        sa.Column("confirmed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("alt_source", sa.String(512), nullable=True),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(["division_code"], ["coverage_divisions.code"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("division_code", "dept", "seq", name="uq_coverage_cells_division_dept"),
        **OPTIONS,
    )
    op.create_index("ix_coverage_cells_state", "coverage_cells", ["state"])
    op.create_index("ix_coverage_cells_dept", "coverage_cells", ["dept"])

    op.create_table(
        "coverage_cell_sources",
        sa.Column("cell_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.ForeignKeyConstraint(["cell_id"], ["coverage_cells.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("cell_id", "source_id"),
        **OPTIONS,
    )

    op.create_table(
        "coverage_acceptances",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("province", sa.String(32), nullable=False),
        sa.Column("level", sa.String(8), nullable=False),
        sa.Column("sampled_by", sa.String(80), nullable=False),
        sa.Column("sampled_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("note", sa.String(300), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        **OPTIONS,
    )
    op.create_index(
        "ix_coverage_acceptances_province", "coverage_acceptances", ["province", "level"]
    )


def downgrade() -> None:
    op.drop_table("coverage_acceptances")
    op.drop_table("coverage_cell_sources")
    op.drop_table("coverage_cells")
    op.drop_table("coverage_divisions")
