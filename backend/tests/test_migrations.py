from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from youju.models import Base


def test_migrations_match_models(db_engine) -> None:
    """迁移脚本建出的表结构必须和模型定义一致，防止改了模型忘记写迁移。"""
    with db_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []
