from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text

from youju.config import get_settings
from youju.db import create_mysql_engine, lock_name

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


@dataclass(frozen=True)
class AheadOfCode:
    """库里有这版代码不认识的迁移号时的情况，给提醒用。"""

    unknown: list[str]  # 库里有、这版代码不认识的迁移号
    known: list[str]  # 库里有、这版代码认识的迁移号（多个 head 时才会有）
    code_heads: list[str]  # 这版代码最新的迁移号


def upgrade(database_url: str | None = None) -> AheadOfCode | None:
    """迁移到这版代码的最新结构，正常时返回 None。库里有这版代码不认识的迁移号（多半是刚把
    admin 回退到旧版本，也可能是误发了落后的分支）时不迁移、返回 AheadOfCode，由调用方提醒；
    原来 alembic 会报"找不到这个版本"，启动脚本直接退出，回退的 admin 起不来，定时任务和
    首屏都停（差距清单 R4）。admin 只有一个实例，起不来就是全停，所以宁可照常启动、立刻提醒：
    提醒里写明两边的迁移号，发版后的检查看到它而当时不是在回退，就是发错了版本。
    加表加列的迁移，旧代码在新结构上照常能跑；删表删列的迁移回退前要先降库（见发版与回退）。"""
    url = database_url or get_settings().database_url
    name = lock_name(_MIGRATION_LOCK)
    engine = create_mysql_engine(url)
    try:
        with engine.connect() as conn:
            # 等前一个实例迁移完再往下走；已是最新结构时 alembic 什么也不做
            got = conn.execute(
                text("SELECT GET_LOCK(:k, :t)"), {"k": name, "t": _LOCK_WAIT_SECONDS}
            ).scalar()
            if got != 1:
                raise RuntimeError(
                    f"等了 {_LOCK_WAIT_SECONDS} 秒仍没拿到迁移锁，另一个实例可能卡住了"
                )
            try:
                cfg = alembic_config(url)
                script = ScriptDirectory.from_config(cfg)
                known = {rev.revision for rev in script.walk_revisions()}
                current = MigrationContext.configure(conn).get_current_heads()
                # 读完马上结束这个事务：事务开着会一直占着 alembic_version 的元数据锁，空库里
                # alembic 要建这张表时会卡死，而迁移锁还拿在手里，别的实例（和测试）也跟着卡。
                # 迁移锁是会话级的，提交不会释放它
                conn.commit()
                unknown = sorted(rev for rev in current if rev not in known)
                if unknown:
                    ahead = AheadOfCode(
                        unknown=unknown,
                        known=sorted(rev for rev in current if rev in known),
                        code_heads=sorted(script.get_heads()),
                    )
                else:
                    ahead = None
                    command.upgrade(cfg, "head")
            finally:
                conn.execute(text("SELECT RELEASE_LOCK(:k)"), {"k": name})
                conn.commit()
    finally:
        engine.dispose()
    return ahead
