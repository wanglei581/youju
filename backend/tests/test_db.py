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


def test_deadline_check_constraints_are_enforced(session) -> None:
    """核对状态的约束在数据库里生效：非法状态、没有核定日期的"通过"、缺原因的"已变更"都写不进去。"""
    from datetime import UTC, date, datetime

    import pytest
    from sqlalchemy.exc import DBAPIError

    from youju.models import Document, DocumentDeadline, Source

    source = Source(
        level="省级",
        province="山东",
        dept="科技",
        dept_full_name="山东省科学技术厅",
        column_name="通知",
        url="https://kjt.shandong.gov.cn/check/",
        enabled=True,
        parser_config={},
    )
    session.add(source)
    session.flush()
    now = datetime.now(UTC)
    doc = Document(
        source_id=source.id,
        url="https://kjt.shandong.gov.cn/check/1.html",
        url_key="kjt.shandong.gov.cn/check/1.html",
        title="关于申报的通知",
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(doc)
    session.commit()

    def deadline(**fields):
        return DocumentDeadline(
            document_id=doc.id,
            fingerprint=str(len(fields)) + "x" * 10,
            kind="企业申报截止",
            candidate_date=date(2026, 10, 15),
            evidence="10月15日前申报",
            confidence="高",
            **fields,
        )

    for bad in (
        {"status": "bogus"},
        {"status": "approved"},  # 通过但没有核定日期
        {"status": "changed", "approved_date": date(2026, 10, 15)},  # 已变更但没写原因和新通知
    ):
        session.add(deadline(**bad))
        with pytest.raises(DBAPIError):
            session.flush()
        session.rollback()
