"""一次性搬迁：把本机 PostgreSQL 开发库的数据原样复制到 MySQL（编号不变）。

只在本机用：换数据库后，核对台里已经核对过的结果、抓过的文件和名单都不用重来。
目标库必须是刚迁移好的空库（`uv run youju db-upgrade` 之后、还没有写入数据）。

用法（在 backend/ 目录下）：
    uv run python scripts/copy_pg_to_mysql.py \\
        --source postgresql+psycopg://localhost:55432/youju \\
        --target "mysql+pymysql://root@127.0.0.1:33306/youju?charset=utf8mb4"
"""

import argparse
import sys

from sqlalchemy import MetaData, create_engine, func, select, text

from youju.db import create_mysql_engine
from youju.models import Base

BATCH = 1000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default="postgresql+psycopg://localhost:55432/youju")
    parser.add_argument(
        "--target", default="mysql+pymysql://root@127.0.0.1:33306/youju?charset=utf8mb4"
    )
    args = parser.parse_args()

    source = create_engine(args.source)
    target = create_mysql_engine(args.target)
    # 按源库的真实类型读：PostgreSQL 的 timestamptz 读出来带时区，写入时由 UTCDateTime 换成 UTC
    source_meta = MetaData()
    source_meta.reflect(source)

    tables = Base.metadata.sorted_tables
    with target.connect() as conn:
        busy = [t.name for t in tables if conn.scalar(select(func.count()).select_from(t))]
    if busy:
        print(f"目标库不是空的，已停止：{', '.join(busy)}", file=sys.stderr)
        return 1

    with source.connect() as src, target.begin() as dst:
        dst.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in tables:
            src_table = source_meta.tables[table.name]
            names = [c.name for c in table.columns]
            rows = src.execute(
                select(*(src_table.c[n] for n in names)).order_by(*src_table.primary_key.columns)
            )
            copied = 0
            while batch := rows.fetchmany(BATCH):
                dst.execute(table.insert(), [dict(zip(names, row, strict=True)) for row in batch])
                copied += len(batch)
            print(f"{table.name}: {copied}")
        dst.execute(text("SET FOREIGN_KEY_CHECKS = 1"))

    # 逐表核对条数
    with source.connect() as src, target.connect() as dst:
        for table in tables:
            a = src.scalar(select(func.count()).select_from(source_meta.tables[table.name]))
            b = dst.scalar(select(func.count()).select_from(table))
            if a != b:
                print(f"条数不一致：{table.name} 源 {a}，目标 {b}", file=sys.stderr)
                return 1
    print("完成：各表条数一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
