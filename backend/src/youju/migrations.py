from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from youju.config import get_settings

BACKEND_DIR = Path(__file__).resolve().parents[2]


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    url = database_url or get_settings().database_url
    # ConfigParser 会把 % 当作插值符号
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


# 迁移时持有的 PostgreSQL 咨询锁编号（任意固定值）：部署时即使同时启动两个实例，也只有一个在迁移
_MIGRATION_LOCK = 2026_0928


def upgrade(database_url: str | None = None) -> None:
    url = database_url or get_settings().database_url
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            # 等前一个实例迁移完再往下走；已是最新结构时 alembic 什么也不做
            conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _MIGRATION_LOCK})
            try:
                command.upgrade(alembic_config(url), "head")
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _MIGRATION_LOCK})
                conn.commit()
    finally:
        engine.dispose()
