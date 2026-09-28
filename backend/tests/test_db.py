"""数据库连接：云托管 MySQL 暂停后恢复期间要重试，其他错误照常报出。"""

import pymysql

from youju.db import _worth_retrying


def test_resuming_and_transient_errors_are_retried() -> None:
    resuming = pymysql.err.OperationalError(
        1105, "CynosDB serverless instance is Resuming, please try connecting again."
    )
    assert _worth_retrying(resuming)
    assert _worth_retrying(pymysql.err.OperationalError(2003, "Can't connect to MySQL server"))
    assert _worth_retrying(pymysql.err.OperationalError(2013, "Lost connection to MySQL server"))


def test_other_errors_are_not_retried() -> None:
    assert not _worth_retrying(pymysql.err.OperationalError(1045, "Access denied for user"))
    assert not _worth_retrying(pymysql.err.OperationalError(1049, "Unknown database"))
    assert not _worth_retrying(ValueError("bad"))


def test_new_connections_use_utc_and_read_committed(db_engine) -> None:
    """时区和隔离级别在驱动建连接时就设好（和建连接一起重试），每个新连接都一样。"""
    from sqlalchemy import text

    with db_engine.connect() as conn:
        zone, isolation = conn.execute(
            text("SELECT @@session.time_zone, @@session.transaction_isolation")
        ).one()
    assert (zone, isolation) == ("+00:00", "READ-COMMITTED")
