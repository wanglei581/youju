from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from youju.config import get_settings
from youju.db import create_mysql_engine

BACKEND_DIR = Path(__file__).resolve().parents[2]


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    url = database_url or get_settings().database_url
    # ConfigParser 会把 % 当作插值符号
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


# 迁移时持有的 MySQL 命名锁：部署时即使同时启动两个实例，也只有一个在迁移
_MIGRATION_LOCK = "youju_migration"
# 等前一个实例迁移完的最长秒数；首次部署建表很快，这里留足余量
_LOCK_WAIT_SECONDS = 600


def upgrade(database_url: str | None = None) -> None:
    url = database_url or get_settings().database_url
    engine = create_mysql_engine(url)
    try:
        with engine.connect() as conn:
            # 等前一个实例迁移完再往下走；已是最新结构时 alembic 什么也不做
            got = conn.execute(
                text("SELECT GET_LOCK(:k, :t)"), {"k": _MIGRATION_LOCK, "t": _LOCK_WAIT_SECONDS}
            ).scalar()
            if got != 1:
                raise RuntimeError(
                    f"等了 {_LOCK_WAIT_SECONDS} 秒仍没拿到迁移锁，另一个实例可能卡住了"
                )
            try:
                command.upgrade(alembic_config(url), "head")
            finally:
                conn.execute(text("SELECT RELEASE_LOCK(:k)"), {"k": _MIGRATION_LOCK})
                conn.commit()
    finally:
        engine.dispose()
