import time
from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from youju.config import get_settings

# 云托管自带的 MySQL 是 Serverless 的：10 分钟没有连接会自动暂停，下次连接要等它恢复，
# 期间可能返回"CynosDB serverless instance is resuming, please try connecting again"，
# 也可能暂时连不上。连接时按这个间隔重试几次（共约 15 秒）。
_CONNECT_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)
# 2003：连不上服务器；2013：连接中途断开
_RETRY_ERROR_CODES = {2003, 2013}


def _worth_retrying(exc: Exception) -> bool:
    if "resuming" in str(exc).lower():
        return True
    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    return code in _RETRY_ERROR_CODES


def create_mysql_engine(url: str, **kwargs) -> Engine:
    """建引擎：会话时区统一为 UTC；用前检查连接是否还活着；MySQL 暂停后恢复期间重试连接。"""
    engine = create_engine(
        url,
        pool_pre_ping=True,
        # 比 MySQL 默认的 wait_timeout（8 小时）短，避免拿到已被服务端关闭的连接
        pool_recycle=3600,
        # 代码按 PostgreSQL 的默认"读已提交"写的：事务里能看到别的连接刚提交的数据。
        # MySQL 默认是"可重复读"，还会多加间隙锁，并发写入时更容易互相等待
        isolation_level="READ COMMITTED",
        **kwargs,
    )

    @event.listens_for(engine, "do_connect")
    def _retry_connect(dialect, conn_rec, cargs, cparams):
        for delay in (*_CONNECT_RETRY_DELAYS, None):
            try:
                return dialect.connect(*cargs, **cparams)
            except Exception as exc:  # 只对"恢复中"和暂时连不上重试，其他错误（如密码错）照常抛出
                if delay is None or not _worth_retrying(exc):
                    raise
                time.sleep(delay)

    @event.listens_for(engine, "connect")
    def _session_utc(dbapi_connection, connection_record):
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET time_zone = '+00:00'")

    return engine


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    return create_mysql_engine(url or get_settings().database_url)


def session_factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个会话。"""
    with session_factory()() as session:
        yield session
