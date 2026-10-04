"""测试夹具。

需要数据库的测试连接 YOUJU_TEST_DATABASE_URL（默认本机 scripts/dev_db.sh 启动的实例）。
数据库不可用时这些测试会跳过；CI 设置 YOUJU_REQUIRE_DB=1，数据库不可用直接失败。
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, make_url, text
from sqlalchemy.orm import Session, sessionmaker

from youju.config import Settings, get_settings
from youju.db import create_mysql_engine, get_engine
from youju.migrations import upgrade
from youju.models import Base

FIXTURES = Path(__file__).parent / "fixtures"
TEST_DB_URL = os.environ.get(
    "YOUJU_TEST_DATABASE_URL", "mysql+pymysql://root@127.0.0.1:33306/youju_test?charset=utf8mb4"
)
# 程序里用默认连接的地方（如每次请求写访问日志）也连测试库，测试不会写到开发库
os.environ["YOUJU_DATABASE_URL"] = TEST_DB_URL
# 测试只使用显式环境配置，避免读取开发环境的密钥或连接信息。
Settings.model_config["env_file"] = None
get_settings.cache_clear()


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    url = make_url(TEST_DB_URL)
    server = create_mysql_engine(url.set(database=""))
    try:
        with server.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001 - 任何连接错误都按"数据库不可用"处理
        server.dispose()
        if os.environ.get("YOUJU_REQUIRE_DB") == "1":
            raise
        pytest.skip(f"测试数据库不可用（{TEST_DB_URL}）：{exc.__class__.__name__}")
    # 每次测试会话从空库开始；排序规则和线上一致（区分大小写、不忽略尾部空格）
    with server.begin() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS `{url.database}`"))
        conn.execute(
            text(f"CREATE DATABASE `{url.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin")
        )
    server.dispose()
    # 用迁移建表，顺便验证迁移脚本本身可用
    upgrade(TEST_DB_URL)
    # 默认连接可能在重建库之前就连上了（连接还指向删掉的库），换一个新的
    get_engine.cache_clear()
    engine = create_mysql_engine(TEST_DB_URL)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(db_engine: Engine) -> sessionmaker[Session]:
    # 清空全部表并让自增编号从 1 开始（MySQL 的 TRUNCATE 会重置自增，但要先关外键检查）
    with db_engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in Base.metadata.sorted_tables:
            conn.execute(text(f"TRUNCATE TABLE `{table.name}`"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    return sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as s:
        yield s


def read_fixture_html(name: str) -> str:
    return (FIXTURES / "html" / f"{name}.html").read_bytes().decode("utf-8", errors="replace")


@pytest.fixture(autouse=True)
def _reset_admin_throttle():
    """管理口令输错计数放在进程内存里，每个测试前清零，免得前面测试的错误次数触发限流。"""
    from youju import access_log
    from youju.api import app as app_module
    from youju.api import exports

    app_module._admin_fails.clear()
    exports._export_times.clear()
    exports._instance_times.clear()
    # 每个测试清空了表，访问日志的密钥也要重新从库里取
    access_log._key = None
    access_log._fail_state.update(last_alert=None, count=0)
    access_log._pending.clear()
    yield
    app_module._admin_fails.clear()
    exports._export_times.clear()
    exports._instance_times.clear()
