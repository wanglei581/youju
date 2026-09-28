"""接口测速（验收 C1：搜索不超过 1.5 秒，首页不超过 1 秒）：在测速库上多次调用小程序接口，
报中位数和 P95（服务端处理时间，不含网络）。

用法（在 backend/ 目录下；--database-url 指向专门的测速库，不要指向开发库或正式库）：
    uv run python scripts/bench_api.py \\
        --database-url "mysql+pymysql://root@127.0.0.1:33306/youju_bench?charset=utf8mb4" --scale 10

--scale N：先把测速库里的文件（连同详情、截止）复制成 N 倍（新编号、新网址），模拟数据量增长。
"""

import argparse
import os
import statistics
import sys
import time
from urllib.parse import quote

OFFSET = 1_000_000  # 复制出的文件编号 = 原编号 + k × OFFSET


def scale(url: str, times: int) -> int:
    from sqlalchemy import func, select, text

    from youju.db import create_mysql_engine
    from youju.models import Document, DocumentDeadline, DocumentDetail

    engine = create_mysql_engine(url)
    with engine.begin() as conn:
        if conn.scalar(select(func.count()).where(Document.id >= OFFSET)):
            print("测速库已经复制过，跳过复制")
            return conn.scalar(select(func.count(Document.id)))
        doc_cols = [c.name for c in Document.__table__.columns]
        detail_cols = [c.name for c in DocumentDetail.__table__.columns]
        deadline_cols = [c.name for c in DocumentDeadline.__table__.columns]

        def copy(table: str, cols: list[str], replace: dict[str, str], where: str) -> None:
            picked = ", ".join(replace.get(c, f"`{c}`") for c in cols)
            names = ", ".join(f"`{c}`" for c in cols)
            conn.execute(
                text(f"INSERT INTO `{table}` ({names}) SELECT {picked} FROM `{table}` WHERE {where}")
            )

        for k in range(1, times):
            off = k * OFFSET
            copy(
                "documents",
                doc_cols,
                {
                    "id": f"id + {off}",
                    "url": f"CONCAT(url, '#bench{k}')",
                    "url_key": f"CONCAT(url_key, '?bench={k}')",
                },
                f"id < {OFFSET}",
            )
            copy(
                "document_details",
                detail_cols,
                {"document_id": f"document_id + {off}"},
                f"document_id < {OFFSET}",
            )
            copy(
                "document_deadlines",
                deadline_cols,
                {"id": f"id + {off}", "document_id": f"document_id + {off}"},
                f"document_id < {OFFSET}",
            )
        return conn.scalar(select(func.count(Document.id)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--scale", type=int, default=1)
    parser.add_argument("--runs", type=int, default=30)
    args = parser.parse_args()
    if "youju_bench" not in args.database_url:
        print("只在名字里带 youju_bench 的测速库上运行", file=sys.stderr)
        return 1
    os.environ["YOUJU_DATABASE_URL"] = args.database_url
    docs = scale(args.database_url, args.scale) if args.scale > 1 else None

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from youju.api.app import app
    from youju.db import session_factory
    from youju.models import Document, ListEntry

    with session_factory()() as session:
        doc_id = session.scalar(select(Document.id).order_by(Document.id.desc()).limit(1))
        name = session.scalar(select(ListEntry.name).limit(1)) or "测试科技有限公司"
    cases = {
        "首页": "/api/v1/home",
        "搜索：不限条件": "/api/v1/search?time=" + quote("不限"),
        "搜索：关键词 申报": "/api/v1/search?time=" + quote("不限") + "&q=" + quote("申报"),
        "搜索：高新技术企业 + 名单": "/api/v1/search?time=" + quote("不限")
        + "&kind=" + quote("名单") + "&q=" + quote("高新技术企业"),
        "搜索：两省 + 申报通知 + 近 30 天": "/api/v1/search?time=" + quote("近 30 天")
        + "&regions=" + quote("山东,广东") + "&types=" + quote("申报通知"),
        "搜索：科技部门 + 公示 + 今年": "/api/v1/search?time=" + quote("今年")
        + "&depts=" + quote("科技部门") + "&status=" + quote("公示"),
        "详情": f"/api/v1/documents/{doc_id}",
        "日历": "/api/v1/calendar",
        "查名单：在名单上": "/api/v1/lists/lookup?q=" + quote(name),
        "查名单：不在名单上": "/api/v1/lists/lookup?q=" + quote("某某不存在科技有限公司"),
    }
    client = TestClient(app)
    if docs:
        print(f"测速库文件数：{docs}")
    print(f"{'接口':<28}{'中位数':>10}{'P95':>10}")
    for label, path in cases.items():
        for _ in range(3):
            res = client.get(path)
            if res.status_code != 200:
                print(f"{label}: HTTP {res.status_code} {res.text[:120]}")
                break
        timings = []
        for _ in range(args.runs):
            start = time.perf_counter()
            client.get(path)
            timings.append((time.perf_counter() - start) * 1000)
        timings.sort()
        p95 = timings[max(0, int(len(timings) * 0.95) - 1)]
        print(f"{label:<28}{statistics.median(timings):>8.0f}ms{p95:>8.0f}ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
