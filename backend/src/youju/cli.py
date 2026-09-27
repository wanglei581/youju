"""命令行：数据库、来源抓取、详情抓取、报告和 API。"""

import argparse
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from youju.config import get_settings
from youju.db import session_factory
from youju.models import Document, Source

log = logging.getLogger("youju")


def _cmd_db_upgrade(_: argparse.Namespace) -> int:
    from youju.migrations import upgrade

    upgrade()
    print("数据库已迁移到最新结构")
    return 0


def _cmd_import_sources(args: argparse.Namespace) -> int:
    from youju.sources.importer import import_sources

    csv_path: Path = args.csv or get_settings().sources_csv
    with session_factory()() as session:
        s = import_sources(session, csv_path)
    print(f"导入完成：新增 {s.created}，更新 {s.updated}；启用 {s.enabled}，暂不抓取 {s.disabled}")
    return 0


def _cmd_crawl(args: argparse.Namespace) -> int:
    from youju.crawler.fetcher import PoliteFetcher
    from youju.crawler.service import crawl_sources

    settings = get_settings()
    factory = session_factory()
    with factory() as session:
        stmt = select(Source.id).order_by(Source.id)
        if not args.include_disabled:
            stmt = stmt.where(Source.enabled.is_(True))
        if args.source_id:
            stmt = stmt.where(Source.id.in_(args.source_id))
        if args.province:
            stmt = stmt.where(Source.province == args.province)
        if args.limit:
            stmt = stmt.limit(args.limit)
        ids = list(session.scalars(stmt).all())
    if not ids:
        print("没有符合条件的来源（先运行 youju import-sources？）")
        return 1
    print(f"开始检查 {len(ids)} 个来源……")
    with PoliteFetcher(
        settings.user_agent, settings.request_timeout, settings.per_host_interval
    ) as fetcher:
        results = crawl_sources(
            factory,
            ids,
            fetcher,
            max_workers=args.workers or settings.max_workers,
            failure_threshold=settings.failure_alert_threshold,
        )
    by_status = Counter(r.status for r in results)
    print("检查结果：" + "，".join(f"{k} {v}" for k, v in by_status.most_common()))
    baseline_new = sum(r.items_new for r in results if r.baseline)
    real_new = sum(r.items_new for r in results if not r.baseline)
    print(
        f"识别到条目 {sum(r.items_found for r in results)} 条；"
        f"新入库 {baseline_new + real_new} 条，其中首次检查的基线 {baseline_new} 条、"
        f"真正新增 {real_new} 条"
    )
    failed = [r for r in results if r.status != "ok"]
    if failed:
        with factory() as session:
            names = {
                s.id: f"{s.province} {s.dept_full_name} {s.column_name}"
                for s in session.scalars(
                    select(Source).where(Source.id.in_([r.source_id for r in failed]))
                )
            }
        print("未成功的来源：")
        for r in failed:
            print(f"  #{r.source_id} {names.get(r.source_id, '')}：{r.status}（{r.error or ''}）")
    return 0


def _cmd_report(_: argparse.Namespace) -> int:
    week_ago = datetime.now(UTC) - timedelta(days=7)
    with session_factory()() as session:
        health = dict(
            session.execute(select(Source.health, func.count()).group_by(Source.health)).all()
        )
        enabled = session.scalar(select(func.count()).where(Source.enabled)) or 0
        total_sources = session.scalar(select(func.count(Source.id))) or 0
        docs = session.scalar(select(func.count(Document.id))) or 0
        new_docs = (
            session.scalar(
                select(func.count(Document.id)).where(
                    Document.is_baseline.is_(False), Document.first_seen_at >= week_ago
                )
            )
            or 0
        )
        failing = session.scalars(
            select(Source)
            .where(Source.health.in_(["failing", "degraded", "blocked"]))
            .order_by(Source.id)
        ).all()
        print(f"来源：共 {total_sources}，启用 {enabled}；健康分布：{health}")
        print(f"文件题录：共 {docs}；近 7 天新发现（不含基线）{new_docs}")
        for s in failing:
            print(
                f"  [{s.health}] #{s.id} {s.province} {s.dept_full_name} {s.column_name}"
                f" 连续失败 {s.consecutive_failures}：{s.last_error or ''}"
            )
    return 0


def _cmd_fetch_details(args: argparse.Namespace) -> int:
    from youju.crawler.detail_service import fetch_details

    with session_factory()() as session:
        results = fetch_details(
            session, limit=args.limit, source_id=args.source_id, max_workers=args.workers
        )
    by_status = Counter(r.status for r in results)
    print(f"处理 {len(results)} 份文件：" + "，".join(f"{k} {v}" for k, v in by_status.items()))
    for result in results:
        if result.status != "ok":
            print(f"  #{result.document_id} {result.status}：{result.failure_reason or ''}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("youju.api.app:app", host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="youju", description="有据数据底座命令行")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("db-upgrade", help="把数据库迁移到最新结构").set_defaults(func=_cmd_db_upgrade)

    p = sub.add_parser("import-sources", help="从全国来源清单 CSV 导入来源")
    p.add_argument("--csv", type=Path, help="默认 docs/数据来源/全国来源清单_P0_v1.csv")
    p.set_defaults(func=_cmd_import_sources)

    p = sub.add_parser("crawl", help="检查来源列表页，发现新条目")
    p.add_argument("--source-id", type=int, action="append", help="只检查指定来源，可重复")
    p.add_argument("--province", help="只检查某个省份（国家级来源填“国家”）")
    p.add_argument("--limit", type=int, help="最多检查多少个来源")
    p.add_argument("--workers", type=int, help="同时抓取的网站数")
    p.add_argument("--include-disabled", action="store_true", help="也检查未启用的来源")
    p.set_defaults(func=_cmd_crawl)

    p = sub.add_parser("fetch-details", help="抓取尚无详情或到期重试的政府文件")
    p.add_argument("--limit", type=int, default=50, help="最多处理多少份文件，默认 50")
    p.add_argument("--source-id", type=int, help="只处理指定来源")
    p.add_argument("--workers", type=int, help="同时抓取的网站数")
    p.set_defaults(func=_cmd_fetch_details)

    sub.add_parser("report", help="打印来源健康和条目统计").set_defaults(func=_cmd_report)

    p = sub.add_parser("serve", help="启动 API 服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=_cmd_serve)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
