"""命令行：数据库、来源抓取、详情抓取、报告和 API。"""

import argparse
import logging
from collections import Counter
from datetime import UTC, date, datetime, timedelta
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
            session,
            limit=args.limit,
            source_id=args.source_id,
            max_workers=args.workers,
            since=args.since,
            notices_only=args.notices_only,
        )
    by_status = Counter(r.status for r in results)
    print(f"处理 {len(results)} 份文件：" + "，".join(f"{k} {v}" for k, v in by_status.items()))
    for result in results:
        if result.status != "ok":
            print(f"  #{result.document_id} {result.status}：{result.failure_reason or ''}")
    return 0


def _cmd_extract_deadlines(args: argparse.Namespace) -> int:
    from youju.extract.service import extract_existing

    with session_factory()() as session:
        documents, added = extract_existing(session, since=args.since)
    print(f"检查 {documents} 份已抓取正文，新增 {added} 条待核对截止候选")
    return 0


def _cmd_ai_check(args: argparse.Namespace) -> int:
    from youju.ai import get_chat_client
    from youju.ai.pipeline import run_ai_checks

    client = get_chat_client()
    if client is None:
        print("没有配置 AI 密钥（YOUJU_AI_API_KEY），跳过 AI 检查，照常用规则")
        return 0
    with client, session_factory()() as session:
        report = run_ai_checks(session, client, limit=args.limit)
    print(f"AI 检查 {report.documents} 份文件（模型 {client.model}）")
    print(
        f"  截止候选：AI 一致 {report.deadlines_agree} 条，只有规则找到 "
        f"{report.deadlines_rule_only} 条，AI 新找到 {report.deadlines_ai_only} 条（都要人工核对）"
    )
    print(f"  类型：和规则一致 {report.kind_agree} 份，不一致 {len(report.kind_disagree)} 份")
    print(
        f"  出错 {report.errors} 份；用量：输入 {report.prompt_tokens}、"
        f"输出 {report.completion_tokens} tokens"
    )
    for doc_id, title, rule, ai_kind in report.kind_disagree[:20]:
        print(f"  类型不一致 #{doc_id} 规则={rule} AI={ai_kind}：{title[:60]}")
    if report.stopped_early:
        print("连续多份文件调用 AI 出错，本轮提前停止（可能是服务不可用或额度用完）")
        return 1
    return 0


def _cmd_parse_lists(args: argparse.Namespace) -> int:
    from youju.lists.service import parse_lists

    with session_factory()() as session:
        report = parse_lists(session, since=args.since)
    print(
        f"名单文件 {report.documents}，成功解析 {report.parsed_documents}，"
        f"企业/机构条目 {report.names}，本次下载附件 {report.attachments_fetched}/200，"
        f"待下次处理附件 {report.attachments_deferred}"
    )
    print("各类别条目数：" + ("，".join(f"{k} {v}" for k, v in report.categories.items()) or "无"))
    print("失败原因分布：" + ("，".join(f"{k} {v}" for k, v in report.failures.items()) or "无"))
    print("抽样（最多10条）：")
    for sample in report.samples:
        print(f"  {sample.title}｜{sample.name}｜{sample.status}｜{sample.url}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("youju.api.app:app", host=args.host, port=args.port)
    return 0


def _cmd_notify(_: argparse.Namespace) -> int:
    from youju.notify import run_notify

    with session_factory()() as session:
        counts = run_notify(session)
    print("提醒任务：" + "，".join(f"{name} {count}" for name, count in counts.items()))
    return 0


def _cmd_cleanup(_: argparse.Namespace) -> int:
    from youju.notify import run_cleanup

    with session_factory()() as session:
        counts = run_cleanup(session)
    print("按保存期限清理：" + "，".join(f"{name} {count} 条" for name, count in counts.items()))
    return 0


def _cmd_alert(args: argparse.Namespace) -> int:
    from youju.alerts import send_alert

    return 1 if send_alert(args.text) is False else 0


def _cmd_check_sources(args: argparse.Namespace) -> int:
    from youju.alerts import send_alert
    from youju.source_health import check_sources

    with session_factory()() as session:
        health = check_sources(session)
    if health.passed:
        log.info("今天栏目检查达标：%s/%s（%.1f%%）", health.successful, health.total, health.rate)
        return 0
    message = health.alert_text()
    log.warning("%s", message)
    sent = send_alert(message)
    # cron 包装器用专用码识别“详细提醒已发”，避免再发一条泛化的任务失败提醒。
    return 20 if args.job and sent is True else 1


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
    p.add_argument("--since", type=date.fromisoformat, help="只处理此发布日期以来的文件")
    p.add_argument(
        "--notices-only", action="store_true", help="只处理标题含申报/征集/推荐/遴选/认定的文件"
    )
    p.set_defaults(func=_cmd_fetch_details)

    p = sub.add_parser("extract-deadlines", help="从已抓取正文生成待核对截止候选")
    p.add_argument("--since", type=date.fromisoformat, help="只处理此发布日期以来的文件")
    p.set_defaults(func=_cmd_extract_deadlines)

    p = sub.add_parser("ai-check", help="AI 做类型判断和截止抽取，和规则互相印证（没配密钥时跳过）")
    p.add_argument("--limit", type=int, default=300, help="本轮最多检查多少份文件")
    p.set_defaults(func=_cmd_ai_check)

    p = sub.add_parser("parse-lists", help="补详情并解析政府名单表格和附件（单次最多200个附件）")
    p.add_argument(
        "--since",
        type=date.fromisoformat,
        default=date(2026, 7, 1),
        help="发布日期起点，默认2026-07-01",
    )
    p.set_defaults(func=_cmd_parse_lists)

    sub.add_parser("report", help="打印来源健康和条目统计").set_defaults(func=_cmd_report)
    sub.add_parser(
        "notify", help="发送一次性订阅提醒；未设置 YOUJU_SEND_WX_MESSAGES=true 时只演练"
    ).set_defaults(func=_cmd_notify)
    sub.add_parser("cleanup", help="按保存期限删除埋点、纠错和已用完的订阅授权").set_defaults(
        func=_cmd_cleanup
    )

    p = sub.add_parser("alert", help="发送一条定时任务文本提醒")
    p.add_argument("text", help="提醒正文")
    p.set_defaults(func=_cmd_alert)

    p = sub.add_parser("check-sources", help="检查今天（北京时间零点起）全部启用栏目的成功率")
    p.add_argument("--job", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=_cmd_check_sources)

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
