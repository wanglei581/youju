"""测试夹具。

需要数据库的测试连接 YOUJU_TEST_DATABASE_URL（默认本机 scripts/dev_db.sh 启动的实例）。
数据库不可用时这些测试会跳过；CI 设置 YOUJU_REQUIRE_DB=1，数据库不可用直接失败。
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from youju.migrations import upgrade

FIXTURES = Path(__file__).parent / "fixtures"
TEST_DB_URL = os.environ.get(
    "YOUJU_TEST_DATABASE_URL", "postgresql+psycopg://localhost:55432/youju_test"
)


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    engine = create_engine(TEST_DB_URL)
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001 - 任何连接错误都按"数据库不可用"处理
        if os.environ.get("YOUJU_REQUIRE_DB") == "1":
            raise
        pytest.skip(f"测试数据库不可用（{TEST_DB_URL}）：{exc.__class__.__name__}")
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    # 用迁移建表，顺便验证迁移脚本本身可用
    upgrade(TEST_DB_URL)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(db_engine: Engine) -> sessionmaker[Session]:
    with db_engine.begin() as conn:
        conn.execute(text("TRUNCATE documents, source_checks, sources RESTART IDENTITY CASCADE"))
    return sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as s:
        yield s


def read_fixture_html(name: str) -> str:
    return (FIXTURES / "html" / f"{name}.html").read_bytes().decode("utf-8", errors="replace")
