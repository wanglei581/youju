"""测试夹具。

需要数据库的测试连接 YOUJU_TEST_DATABASE_URL（默认本机 scripts/dev_db.sh 启动的实例）。
数据库不可用时这些测试会跳过；CI 设置 YOUJU_REQUIRE_DB=1，数据库不可用直接失败。
"""

import os
import re
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
    # 此夹具会重建整个库，先拒绝非测试库，不能依赖调用者命名自觉。
    assert url.database and re.fullmatch(r"youju_test[A-Za-z0-9_]*", url.database)
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
    # 换了一批数据：清掉截止摘录的整批缓存（按日期和截止数据版本做键，清空后自增编号会重复）
    from youju.api import v1

    v1._EXCERPT_CACHE.clear()
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
    from youju.api import exports, pay, v1

    # 订单主动核对的频率（同一张 2 秒内只问一次微信）也在进程内存里；订单编号每个测试从 1 重新数
    pay._checked.clear()
    v1._LOOKUP_TIMES.clear()
    v1._LOOKUP_INSTANCE.clear()
    # 规则列算完没有：测试里每次都重新查，中途把列改成 NULL（模拟刚升级）马上生效
    v1._rules_complete, v1._rules_checked_at, v1._RULES_RECHECK = False, 0.0, 0.0
    app_module._admin_fails.clear()
    # 成批失败的计数在进程内：上一个测试故意制造的 5xx 不能算到下一个测试头上
    app_module._failed_state["recent"].clear()
    app_module._failed_state["last_alert"] = None
    exports._export_times.clear()
    exports._instance_times.clear()
    # 每个测试清空了表，访问日志的密钥也要重新从库里取
    access_log._key = None
    access_log._fail_state.update(last_alert=None, count=0)
    access_log._pending.clear()
    yield
    v1._LOOKUP_TIMES.clear()
    v1._LOOKUP_INSTANCE.clear()
    v1._rules_complete, v1._rules_checked_at, v1._RULES_RECHECK = False, 0.0, 60.0
    app_module._admin_fails.clear()
    exports._export_times.clear()
    exports._instance_times.clear()


@pytest.fixture
def restricted_api_factory(db_engine):
    """按部署文件授权；with factory(new_grants=False) 模拟迁移后尚未追加授权。"""
    import secrets
    from contextlib import contextmanager

    from sqlalchemy.exc import DBAPIError

    @contextmanager
    def factory(*, new_grants=True):
        url = db_engine.url
        assert url.database.startswith("youju_test")
        password = secrets.token_urlsafe(24)
        account = {"test_user": "youju_api_test"}
        created = False
        engine = None
        try:
            try:
                with db_engine.begin() as conn:
                    # CI通过Docker网桥连接；以服务器实际看到的来源建账号，不开放任意主机。
                    account["test_host"] = conn.scalar(
                        text("SELECT SUBSTRING_INDEX(USER(), '@', -1)")
                    )
                    conn.execute(
                        text("CREATE USER :test_user@:test_host IDENTIFIED BY :password"),
                        {**account, "password": password},
                    )
                created = True
            except DBAPIError as exc:
                code = exc.orig.args[0]
                if code in (1044, 1045, 1142, 1227):
                    if os.environ.get("YOUJU_REQUIRE_DB") == "1":
                        raise
                    pytest.skip(
                        f"测试 root 无 CREATE USER 权限（MySQL {code}），受限账号测试未运行"
                    )
                raise
            sql = (Path(__file__).parents[1] / "deploy/mysql_accounts.sql").read_text()
            if not new_grants:
                sql = sql.split("-- 支付权限：", 1)[0]
            grants = [
                stmt
                for stmt in re.findall(r"^GRANT\b[^;]*;", sql, re.MULTILINE)
                if re.search(r"TO\s+'youju_api'@", stmt)
            ]
            assert grants
            with db_engine.begin() as conn:
                for stmt in grants:
                    stmt = re.sub(
                        r"\bON youju\.", "ON " + chr(96) + url.database + chr(96) + ".", stmt
                    )
                    stmt = stmt.replace("'youju_api'@'%'", ":test_user@:test_host")
                    conn.execute(text(stmt), account)
            engine = create_mysql_engine(url.set(username="youju_api_test", password=password))
            yield sessionmaker(bind=engine, expire_on_commit=False)
        finally:
            if engine is not None:
                engine.dispose()
            if created:
                with db_engine.begin() as conn:
                    conn.execute(text("DROP USER :test_user@:test_host"), account)

    return factory
