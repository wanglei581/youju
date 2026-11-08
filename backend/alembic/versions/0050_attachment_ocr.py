"""扫描页识别（D-125）：附件文字的内容版本加两列，只新增。

- attachment_text_versions.ocr_rules：按哪一版识别规则识别过扫描页（attachment_text.OCR_RULES）。
  0 是没识别过；平常的取文字重取一份内容时也写回 0（原来识别出的字跟着换掉了，识别任务再来）。
- attachment_text_versions.ocr_pages：识别出文字的扫描页有几页（scanned_pages 是识别以后还没认出的页）。

两列都有默认值 0，旧代码不读也不写它们：先发 admin（迁移）再发 api、或者反过来都行；回退代码不用降库。
降库（downgrade）删掉这两列：识别出的字和每段识别页前面那一行说明还在段落里，详情页的说法回到
旧的（只按还没认出的页写）。要回退时先关开关（YOUJU_OCR_ENABLED），再按《发版与回退》降库。

编号 0050：0047 留给问一问第二批，0048 是覆盖看板（#460），0049 是 D-123 的保存（#461）；它们都还没合并，
down_revision 先接 main 上最新的 0046，谁后合并谁改成接在前一个后面。

Revision ID: 0050
Revises: 0046
"""

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "attachment_text_versions",
        sa.Column("ocr_rules", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "attachment_text_versions",
        sa.Column("ocr_pages", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("attachment_text_versions", "ocr_pages")
    op.drop_column("attachment_text_versions", "ocr_rules")
