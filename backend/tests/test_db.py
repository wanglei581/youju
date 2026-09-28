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
