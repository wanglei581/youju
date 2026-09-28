"""MySQL 专用的列类型（线上用微信云托管自带的 MySQL 8.0）。"""

from datetime import UTC, datetime

from sqlalchemy import JSON, text
from sqlalchemy.dialects.mysql import DATETIME, MEDIUMTEXT
from sqlalchemy.types import TypeDecorator

# 正文可能超过 TEXT 的 64KB 上限（开发库最长约 40 万字节）；MEDIUMTEXT 上限 16MB
LongText = MEDIUMTEXT
JSONType = JSON

# 与 UTCDateTime 精度一致的建表默认值（MySQL 要求默认值和列的小数位数相同）
NOW = text("CURRENT_TIMESTAMP(6)")


class UTCDateTime(TypeDecorator):
    """MySQL 的 DATETIME 不带时区：一律按 UTC 存，读出来补上 UTC 时区。

    程序里的时间都是带时区的；没带时区的按 UTC 理解（数据库会话时区也设为 UTC）。
    保留微秒，和原来 PostgreSQL 的 timestamptz 精度一致。
    """

    impl = DATETIME(fsp=6)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value.replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)
