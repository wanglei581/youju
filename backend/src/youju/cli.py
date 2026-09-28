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


def _finish_ai(client, task: str) -> bool:
    """命令结束时把这次的 AI 用量记到台账，再关掉客户端。记账失败重试一次，还失败就把没记上的
    用量打到日志里并返回 False：命令按失败退出、由 job.sh 提醒，免得 AI 花费悄悄漏记。"""
    if client is None:
        return True
    try:
        if not client.totals["calls"]:
            return True
        from youju.ai.usage import record_usage

        for attempt in (1, 2):
            try:
                with session_factory()() as session:
                    record_usage(session, client, task)
                return True
            except Exception:
                log.exception("AI 用量记账失败（%s，第 %d 次）", task, attempt)
        totals = client.totals
        print(
            f"AI 用量没记进台账（{task}）：调用 {totals['calls']} 次、出错 {totals['errors']} 次、"
            f"输入 {totals['prompt_tokens']}、输出 {totals['completion_tokens']} tokens"
        )
        return False
    finally:
        client.close()


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
    usage_ok = False
    try:
        with session_factory()() as session:
            report = run_ai_checks(session, client, limit=args.limit)
    finally:
        usage_ok = _finish_ai(client, "check")
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
    return 0 if usage_ok else 1


def _cmd_audit_deadlines(args: argparse.Namespace) -> int:
    from youju.ai import get_chat_client
    from youju.ai.audit import alert_text, run_audit
    from youju.alerts import send_alert

    client = get_chat_client()
    usage_ok = False
    try:
        with session_factory()() as session:
            report = run_audit(session, client, sample=args.sample)
    finally:
        usage_ok = _finish_ai(client, "audit")
    ai_part = (
        f"AI 抽查 {report.checked_by_ai} 份文件"
        if client is not None
        else "没配 AI 密钥，只做规则回查"
    )
    print(f"质检：规则回查 {report.checked_by_rules} 条已发布的截止，{ai_part}")
    print(
        f"  新发现存疑 {len(report.flagged)} 条；"
        f"AI 说有误但对不上原文、没采信 {report.unverified} 条；AI 出错 {report.errors} 份；"
        f"用量：输入 {report.prompt_tokens}、输出 {report.completion_tokens} tokens"
    )
    for deadline_id, title, note in report.flagged[:20]:
        print(f"  #{deadline_id} {title[:40]}：{note}")
    if report.stopped_early:
        print("连续多份文件调用 AI 出错，AI 抽查提前停止（规则回查已做完）")
    # 存疑和 AI 出错合成一条提醒；规则回查照常做完，所以不按任务失败退出（用量没记上除外）
    text = alert_text(report)
    if text:
        send_alert(text)
    return 0 if usage_ok else 1


def _cmd_daily_report(args: argparse.Namespace) -> int:
    from youju.ai import get_chat_client
    from youju.alerts import send_alert
    from youju.digest import build_digest, digest_text, pick_for_review

    client = get_chat_client()
    usage_ok = False
    now = datetime.now(UTC)  # 日报和 AI 建议用同一个时间，跨午夜也按同一天算
    try:
        with session_factory()() as session:
            digest = build_digest(session, now=now)
            # 配了 AI 时请它从待核对的通知里挑几份建议先核对；没配或出错时日报照发
            picks = pick_for_review(session, client, now=now) if client is not None else []
    finally:
        usage_ok = _finish_ai(client, "report")
    text = digest_text(digest, picks)
    print(text)
    sent = None if args.dry_run else send_alert(text)
    return 1 if sent is False or not usage_ok else 0


def _cmd_backup(args: argparse.Namespace) -> int:
    from youju.backup import BackupError, dump, run_backup
    from youju.db import create_mysql_engine

    engine = create_mysql_engine(get_settings().database_url)
    try:
        if args.output:
            with args.output.open("wb") as out:
                counts = dump(engine, out)
            size = args.output.stat().st_size
            print(f"已导出到 {args.output}（{size} 字节）：共 {sum(counts.values())} 条记录")
            return 0
        env = get_settings().cloud_env
        if not env:
            print("没有设置 YOUJU_CLOUD_ENV（云托管环境 ID），无法上传备份")
            return 1
        report = run_backup(engine, env)
    except BackupError as exc:
        print(f"备份失败：{exc}")
        return 1
    finally:
        engine.dispose()
    print(
        f"已备份 {sum(report.counts.values())} 条记录（{report.bytes} 字节）到 {report.path}；"
        f"删掉 {report.pruned} 份 30 天前的备份"
    )
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    from youju.backup import BackupError, restore
    from youju.db import create_mysql_engine

    engine = create_mysql_engine(get_settings().database_url)
    try:
        with args.file.open("rb") as src:
            counts = restore(engine, src)
    except BackupError as exc:
        print(f"恢复失败：{exc}")
        return 1
    finally:
        engine.dispose()
    print(f"已恢复 {sum(counts.values())} 条记录，各表条数和备份一致：")
    for name, count in counts.items():
        print(f"  {name}: {count}")
    return 0


def _cmd_triage_feedback(args: argparse.Namespace) -> int:
    from youju.ai import get_chat_client
    from youju.ai.triage import triage_feedback

    client = get_chat_client()
    usage_ok = False
    try:
        with session_factory()() as session:
            report = triage_feedback(session, client, limit=args.limit)
    finally:
        usage_ok = _finish_ai(client, "triage")
    print(
        f"纠错分诊 {report.triaged} 条，新标存疑 {report.flagged} 条"
        + ("" if client is not None else "（没配 AI 密钥，只做规则回查）")
        + f"；AI 出错 {report.errors} 次"
    )
    if report.stopped_early:
        print("连续多条调用 AI 出错，本轮提前停止，下一轮再试")
        return 1
    return 0 if usage_ok else 1


def _cmd_try_source(args: argparse.Namespace) -> int:
    from youju.ai import get_chat_client
    from youju.sources.onboard import trial, trial_fetcher

    client = get_chat_client()
    usage_ok = False
    try:
        with trial_fetcher() as fetcher:
            result = trial(args.url, fetcher, client, want_ai_rule=args.ai_rule)
    finally:
        usage_ok = _finish_ai(client, "onboard")
    print(f"抓取：{result.status}（{result.final_url}）{result.error or ''}")
    if result.status != "ok":
        return 1
    print(f"通用规则识别出 {len(result.generic)} 条：")
    for item in result.generic[:5]:
        print(f"  {item.publish_date} {item.title[:60]}")
    if result.ai_config:
        print(f"AI 写的规则 {result.ai_config} 识别出 {len(result.ai_items)} 条：")
        for item in result.ai_items[:5]:
            print(f"  {item.publish_date} {item.title[:60]}")
    elif result.ai_note:
        print(f"AI 写的规则没采用：{result.ai_note}")
    p = result.proposal
    print(
        f"登记建议：层级 {p.level or '？'}，省份 {p.province or '？'}，部门 {p.dept or '？'}，"
        f"部门全称 {p.dept_full_name or '？'}，栏目 {p.column_name or '？'}"
    )
    print('只是试跑，没有写数据库；到后台"来源接入"页确认后才接入')
    return 0 if usage_ok else 1


def _cmd_parse_lists(args: argparse.Namespace) -> int:
    from youju.ai import get_chat_client
    from youju.lists.service import parse_lists

    # 配了 AI 时，规则找不到名称列的表格请 AI 认列；没配就只用规则
    client = get_chat_client()
    usage_ok = False
    try:
        with session_factory()() as session:
            report = parse_lists(session, since=args.since, ai_client=client)
    finally:
        usage_ok = _finish_ai(client, "lists")
    print(
        f"名单文件 {report.documents}，成功解析 {report.parsed_documents}，"
        f"企业/机构条目 {report.names}，本次下载附件 {report.attachments_fetched}/200，"
        f"待下次处理附件 {report.attachments_deferred}"
    )
    if report.ai_tables_tried:
        print(
            f"规则找不到名称列、请 AI 认列的表格 {report.ai_tables_tried} 张，"
            f"AI 认出并抽到机构名称的 {report.ai_tables_found} 张"
        )
    print("各类别条目数：" + ("，".join(f"{k} {v}" for k, v in report.categories.items()) or "无"))
    print("失败原因分布：" + ("，".join(f"{k} {v}" for k, v in report.failures.items()) or "无"))
    print("抽样（最多10条）：")
    for sample in report.samples:
        print(f"  {sample.title}｜{sample.name}｜{sample.status}｜{sample.url}")
    return 0 if usage_ok else 1


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

    p = sub.add_parser(
        "audit-deadlines",
        help="质检已发布的截止日期：规则回查全部，配了 AI 时再抽查一部分；存疑的提醒到群里",
    )
    p.add_argument("--sample", type=int, default=20, help="AI 本次最多抽查多少份文件")
    p.set_defaults(func=_cmd_audit_deadlines)

    p = sub.add_parser("backup", help="备份数据库：导出整库，上传到云托管对象存储，保留 30 天")
    p.add_argument("--output", type=Path, help="只导出到本机这个文件，不上传")
    p.set_defaults(func=_cmd_backup)

    p = sub.add_parser(
        "restore", help="把备份文件恢复到当前数据库（要已迁移到同一版本、而且是空的）"
    )
    p.add_argument("file", type=Path, help="备份文件（.jsonl.gz）")
    p.set_defaults(func=_cmd_restore)

    p = sub.add_parser(
        "triage-feedback",
        help="纠错分诊：用规则和 AI 复查纠错对应文件已发布的截止日期（纠错内容不发给 AI）",
    )
    p.add_argument("--limit", type=int, default=50, help="本轮最多分诊多少条纠错")
    p.set_defaults(func=_cmd_triage_feedback)

    p = sub.add_parser(
        "try-source", help="试跑一个栏目网址：抓一次列表页、识别条目、给出登记建议（不写数据库）"
    )
    p.add_argument("url", help="政府网站栏目列表页网址（域名以 .gov.cn 结尾）")
    p.add_argument("--ai-rule", action="store_true", help="通用规则识别出来了也让 AI 写抓取规则")
    p.set_defaults(func=_cmd_try_source)

    p = sub.add_parser("parse-lists", help="补详情并解析政府名单表格和附件（单次最多200个附件）")
    p.add_argument(
        "--since",
        type=date.fromisoformat,
        default=date(2026, 7, 1),
        help="发布日期起点，默认2026-07-01",
    )
    p.set_defaults(func=_cmd_parse_lists)

    sub.add_parser("report", help="打印来源健康和条目统计").set_defaults(func=_cmd_report)
    p = sub.add_parser(
        "daily-report",
        help="运维日报：运行情况、待办和 AI 用量发到群里，配了 AI 时附上建议先核对的通知",
    )
    p.add_argument("--dry-run", action="store_true", help="只打印，不发到群里")
    p.set_defaults(func=_cmd_daily_report)
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
