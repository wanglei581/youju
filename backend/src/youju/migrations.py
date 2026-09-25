from pathlib import Path

from alembic import command
from alembic.config import Config

from youju.config import get_settings

BACKEND_DIR = Path(__file__).resolve().parents[2]


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    url = database_url or get_settings().database_url
    # ConfigParser 会把 % 当作插值符号
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade(database_url: str | None = None) -> None:
    command.upgrade(alembic_config(database_url), "head")
