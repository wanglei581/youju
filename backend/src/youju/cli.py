"""命令行：数据库、来源抓取、详情抓取、报告和 API。"""

import argparse
import json
import logging
import sys
import tempfile
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

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
    from youju.alerts import send_alert
    from youju.migrations import upgrade

    ahead = upgrade()
    if ahead is None:
        print("数据库已迁移到最新结构")
        return 0
    # 库比代码新：不迁移、照常启动（回退的 admin 不能因此起不来），提醒一声；提醒本身出任何错
    # 都不能让这条命令失败（启动脚本 set -e，失败就起不来了）
    message = (
        f"启动时发现库里的迁移版本 {'、'.join(ahead.unknown)} 比这版代码新"
        f"（这版代码最新是 {'、'.join(ahead.code_heads) or '无'}），没有迁移，照常启动。"
        "刚回退过就不用管；当时不是在回退，就是发错了版本，要立即停下来查。"
        + (
            f"库里另有这版代码认识的迁移版本 {'、'.join(ahead.known)}，也没有补迁移。"
            if ahead.known
            else ""
        )
        + "删表删列的迁移回退前要先降库，见《发版与回退》"
    )
    log.warning("%s", message)
    try:
        sent = send_alert(message)
    except Exception as exc:  # noqa: BLE001 - 提醒发不出去不能拦住启动；只记类型
        log.warning("库比代码新的提醒没发出去：%s", type(exc).__name__)
    else:
        if sent is False:
            log.warning("库比代码新的提醒没发出去")
    print(f"数据库比代码新（{'、'.join(ahead.unknown)}），没有迁移")
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
    # 正常失败已有检查记录，仍让 round.sh 继续核对；记账失败则不能用旧记录判成功。
    if any(not r.recorded for r in results):
        print("部分来源的失败记录未能保存，本轮检查未完成")
        return 1
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


def _cmd_rescan_list_details(args: argparse.Namespace) -> int:
    """一次性：重抓还没读出企业名的名单详情，补上按新规则才认得出的附件；默认只预览。"""
    from youju.lists.rescan import file_server_suffixes, rescan_list_details

    suffixes = args.host_suffix or file_server_suffixes()
    with session_factory()() as session:
        results = rescan_list_details(
            session, apply=args.apply, limit=args.limit, suffixes=suffixes
        )
    gained = [r for r in results if r.new_attachments]
    failed = [r for r in results if r.reason and not r.new_attachments]
    for r in results:
        if r.new_attachments:
            print(f"  #{r.document_id} {r.title}")
            for name, url in r.new_attachments:
                print(f"      + {name}  {url}")
            if r.reason:
                print(f"      ！{r.reason}")
        elif r.reason:
            print(f"  #{r.document_id} {r.reason}：{r.title}")
    verb = "新记下" if args.apply else "会新认出"
    print(
        f"{'已写入' if args.apply else '预览（未写入）'}：检查 {len(results)} 份名单"
        f"（{'、'.join(suffixes)}），{verb}附件的 {len(gained)} 份，"
        f"没有新附件的 {len(results) - len(gained) - len(failed)} 份，"
        f"没抓成功或未写入的 {len(failed)} 份"
    )
    if args.apply and gained:
        print("下一轮名单解析（youju parse-lists）会读这些附件。")
    elif not args.apply and gained:
        print("加 --apply 写入（只追加新附件，不改正文和原有附件）")
    if len(results) == args.limit:
        print(f"已到 --limit {args.limit} 上限，可能还有没检查的，调大 --limit 再跑")
    return 0


def _short_lines(lines: list[str]) -> str:
    return " | ".join(line[:30] for line in lines[:3]) + (" …" if len(lines) > 3 else "")


def _impact_text(impact) -> str:
    parts = [
        (impact.approved_fields, "已核对的支持方式 {} 条（转待重新核对）"),
        (impact.confirmed_conditions, "已确认的申报条件 {} 份（不再对外，重抽后重新确认）"),
        (impact.confirmed_reposts, "确认过的转发组成员 {} 个（组转待复核）"),
        (impact.ai_results, "用户的 AI 解读结果 {} 份（显示“通知正文已变化，请重新解读”）"),
        (impact.lost_quotes, "已核对截止的原句在新正文里没有了 {} 条（要人看）"),
        (impact.ai_checks, "按旧正文做的后台 AI 复查 {} 项（删掉，下一轮重查）"),
    ]
    return "，".join(text.format(n) for n, text in parts if n) or "没有"


def _cmd_refetch_bodies(args: argparse.Namespace) -> int:
    """一次性：重新打开原网页，按现在的规则重取正文（10/22 正文混进网页上的字）；默认只预览。"""
    from youju.crawler.body_refresh import Impact, refresh_bodies, survey

    if args.survey:
        # 只读、不联网：按来源数正文带网页字样的份数，挑要重抓的网站
        with session_factory()() as session:
            rows = survey(session)
        for row in rows:
            if not row.hits:
                continue
            s = row.source
            host = (urlsplit(s.url).hostname or "").lower()
            print(
                f"  #{s.id} {s.province} {s.dept_full_name} {s.column_name}（{host}）："
                f"抓成功 {row.ok} 份，正文带网页字样 {row.hits} 份"
            )
            print(f"refetch_bodies survey source={s.id} host={host} ok={row.ok} hits={row.hits}")
        print(
            f"共 {len(rows)} 个来源，{sum(1 for r in rows if r.hits)} 个有带网页字样的正文，"
            f"合计 {sum(r.hits for r in rows)} 份；逐个网站先预览：youju refetch-bodies --host 网站"
        )
        return 0
    if not args.host and not args.source_id:
        print("要用 --host 或 --source-id 指定网站或来源（不整库重抓）", file=sys.stderr)
        return 2
    with session_factory()() as session:
        results = refresh_bodies(
            session,
            hosts=args.host or [],
            source_ids=args.source_id or [],
            apply=args.apply,
            include_changed=args.include_changed,
            limit=args.limit,
            after_id=args.after_id,
            max_workers=args.workers,
        )
    totals = {"trimmed": Impact(), "changed": Impact()}
    for r in results:
        if r.result == "trimmed":
            print(f"  #{r.document_id} {r.title}")
            if r.head:
                print(f"      去掉开头 {len(r.head)} 行：{_short_lines(r.head)}")
            if r.tail:
                print(f"      去掉结尾 {len(r.tail)} 行：{_short_lines(r.tail)}")
        elif r.result == "changed":
            print(f"  #{r.document_id} {r.title}")
            print(f"      其他变化：原来「{r.old_start}」→ 现在「{r.new_start}」")
        elif r.result in ("empty", "failed"):
            print(f"  #{r.document_id} 没换（{r.reason}）：{r.title}")
        if r.result in totals:
            totals[r.result].add(r.impact)
            if any(vars(r.impact).values()):
                print(f"      受影响：{_impact_text(r.impact)}")
        if r.written:
            print("      已写入")
        elif r.reason and r.result in totals:
            print(f"      ！{r.reason}")
    by_result = Counter(r.result for r in results)
    written = sum(r.written for r in results)
    print(
        f"{'已写入' if args.apply else '预览（未写入）'}：检查 {len(results)} 份，"
        f"只去掉网页上的字 {by_result['trimmed']} 份，其他变化 {by_result['changed']} 份，"
        f"没变 {by_result['unchanged']} 份，新正文为空 {by_result['empty']} 份，"
        f"没抓成功 {by_result['failed']} 份；写入 {written} 份"
    )
    print(f"只去掉网页上的字的，写入后受影响：{_impact_text(totals['trimmed'])}")
    print(
        f"其他变化的（加 --include-changed 才换），写入后受影响：{_impact_text(totals['changed'])}"
    )
    if not args.apply and (by_result["trimmed"] or by_result["changed"]):
        print("加 --apply 写入（只换正文和抓取时间）；其他变化的要再加 --include-changed")
    last_id = results[-1].document_id if results else args.after_id or 0
    if len(results) == args.limit:
        print(f"已到 --limit {args.limit} 上限，可能还有没检查的，用 --after-id {last_id} 接着跑")
    # 容器终端里复制出来的中文可能有乱码，以这一行为准（只有英文和数字）
    total = Impact()
    for impact in totals.values():
        total.add(impact)
    print(
        f"refetch_bodies mode={'apply' if args.apply else 'preview'} checked={len(results)} "
        + " ".join(f"{k}={by_result[k]}" for k in ("trimmed", "changed", "unchanged", "empty"))
        + f" failed={by_result['failed']} written={written} last_id={last_id} "
        + " ".join(f"{k}={v}" for k, v in vars(total).items())
    )
    return 0


def _cmd_backfill_titles(args: argparse.Namespace) -> int:
    """一次性补全列表页截断的历史标题；默认只预览，加 --apply 才写入。"""
    from youju.classify import doc_kind
    from youju.crawler.detail_service import backfill_truncated_titles

    with session_factory()() as session:
        results = backfill_truncated_titles(
            session,
            apply=args.apply,
            limit=args.limit,
            source_id=args.source_id,
            since=args.since,
            after_id=args.after_id,
            max_workers=args.workers,
        )
    by_result = Counter(r.result for r in results)
    kind_changed = 0
    for r in results:
        if r.result == "filled" and r.new_title:
            before, after = doc_kind(r.old_title), doc_kind(r.new_title)
            kind_changed += before != after
            change = f"（类型 {before} → {after}）" if before != after else ""
            print(f"  #{r.document_id} {r.old_title}\n      → {r.new_title}{change}")
        elif r.result == "no_match":
            seen = "｜".join(t[:60] for t in r.page_titles[:3]) or "无"
            print(f"  #{r.document_id} 对不上，不改：{r.old_title}（页面标题：{seen}）")
        elif r.result == "unavailable":
            print(f"  #{r.document_id} 详情页不可用（{r.reason}），不改：{r.old_title}")
        elif r.result == "not_truncated":
            print(f"  #{r.document_id} 清理后不是截断标题，不改（youju reclassify 会清理）")
        else:
            print(f"  #{r.document_id} 题录已删除")
    print(
        f"{'已写入' if args.apply else '预览（未写入）'}：截断标题 {len(results)} 份，"
        f"{'已补全' if args.apply else '可补全'} {by_result['filled']} 份"
        f"（其中按标题算的类型会变 {kind_changed} 份），对不上不改 {by_result['no_match']} 份，"
        f"详情页不可用 {by_result['unavailable']} 份，"
        f"清理后不是截断标题 {by_result['not_truncated']} 份"
    )
    if len(results) == args.limit:
        last = results[-1].document_id
        print(f"已到 --limit {args.limit} 份，后面可能还有：加 --after-id {last} 接着处理")
    if not args.apply:
        print("确认无误后加 --apply 写入；写入时按库里当时的标题重新判断")
    return 0


def _cmd_extract_deadlines(args: argparse.Namespace) -> int:
    from youju.extract.service import extract_existing

    with session_factory()() as session:
        documents, added = extract_existing(session, since=args.since)
    print(f"检查 {documents} 份已抓取正文，新增 {added} 条待核对截止候选")
    return 0


def _cmd_deadline_excerpts(args: argparse.Namespace) -> int:
    """只读抽查：仅展示汇总，不输出正文或候选日期。"""
    from youju.api import v1
    from youju.classify import effective_kind
    from youju.deadline_excerpts import batch
    from youju.models import DocumentDeadline

    with session_factory()() as session:
        candidates = list(
            session.scalars(
                select(DocumentDeadline.document_id)
                .where(DocumentDeadline.status.in_(("pending", "approved")))
                .distinct()
                .order_by(DocumentDeadline.document_id.desc())
                .limit(args.sample * 10)
            )
        )
        raw = [
            doc.id
            for doc, _ in session.execute(
                v1._base().where(Document.id.in_(candidates)).order_by(Document.id.desc())
            )
            if effective_kind(doc.title, doc.kind_override) == "申报通知"
        ][: args.sample]
        approved = v1._approved(session, raw)
        views, _, reasons = batch(session, raw, approved, v1.now().date())
    counts = Counter(view.source for entries in views.values() for view in entries)
    names = {"verified": "已核对", "excerpt": "机器摘录", "unverified": "不写日期"}
    print(
        f"抽查 {len(raw)} 份通知，按截止类型计："
        + "，".join(f"{names[source]} {counts[source]}" for source in names)
    )
    print(
        "不写日期的原因："
        + "，".join(
            f"{reason} {reasons[reason]}"
            for reason in (
                "7 天内",
                "30 天以后",
                "过期",
                "冲突",
                "日期不在原句里",
                "原句不在正文里",
            )
        )
    )
    return 0


def _cmd_repost_groups(_: argparse.Namespace) -> int:
    """按转发归组规则增量更新转发组（D-055）：新组和新加入的版本都待确认，确认后才对外合并。"""
    from youju.repost import rebuild

    with session_factory()() as session:
        summary = rebuild(session)
        session.commit()
    print(
        f"转发归组：规则提出 {summary.proposals} 组；新建 {summary.created} 组，新加入 "
        f"{summary.joined} 个版本，去掉规则不再支持的 {summary.dropped} 个，转给主文件 "
        f"{summary.moved} 个；需复核 {summary.needs_review} 组，所有组等人工确认"
    )
    return 0


def _cmd_extract_fields(args: argparse.Namespace) -> int:
    from youju.ai import get_chat_client
    from youju.fields import extract_existing

    client = get_chat_client()
    usage_ok = False
    try:
        with session_factory()() as session:
            report = extract_existing(session, since=args.since, ai_client=client)
    finally:
        usage_ok = _finish_ai(client, "fields")
    print(
        f"检查 {report.documents} 份申报通知正文，新增或更新 {report.fields} 条字段候选，"
        f"正文变化 {report.changed} 份"
    )
    return 0 if usage_ok else 1


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
            report = run_ai_checks(session, client, limit=args.limit, due_first=args.due_first)
    finally:
        usage_ok = _finish_ai(client, "check")
    print(f"AI 检查 {report.documents} 份文件（模型 {client.model}）")
    if args.due_first is not None:
        print(
            f"  其中先补跑 {report.due_soon_documents} 份："
            f"有 {args.due_first} 天内截止的待核对候选、AI 还没抽过截止"
        )
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
    if report.body_changed:
        print(f"  检查期间正文被换掉、这次没记的 {report.body_changed} 份（下一轮按新正文重查）")
    if report.stopped_early:
        print("连续多份文件调用 AI 出错，本轮提前停止（可能是服务不可用或额度用完）")
        return 1
    return 0 if usage_ok else 1


def _cmd_extract_conditions(args: argparse.Namespace) -> int:
    from youju.ai import get_chat_client
    from youju.conditions import run_extraction

    # 一份清单输出较长（每条带原文原句），单次请求多等一会儿
    client = get_chat_client(timeout=90.0)
    if client is None:
        print("没有配置 AI 密钥（YOUJU_AI_API_KEY），跳过申报条件抽取")
        return 0
    usage_ok = False
    try:
        with session_factory()() as session:
            report = run_extraction(session, client, limit=args.limit, due_days=args.due_days)
    finally:
        usage_ok = _finish_ai(client, "conditions")
    print(
        f"申报条件：抽取 {report.documents} 份申报通知（模型 {client.model}），"
        f"其中 {args.due_days} 天内截止的 {report.due_soon} 份排在前面"
    )
    print(
        f"  送核对 {report.lists} 份、{report.items} 条（都要人工核对、整份确认后才对外）；"
        f"没有条目的 {report.empty} 份"
    )
    reasons = "、".join(f"{k} {v}" for k, v in sorted(report.dropped_reasons.items()))
    print(f"  没通过原文核对、已丢弃 {report.dropped} 条{f'（{reasons}）' if reasons else ''}")
    if report.revived:
        print(f"  正文改回以前那一版、恢复旧清单 {report.revived} 份（明细见日志）")
    print(
        f"  出错 {report.errors} 份；用量：输入 {report.prompt_tokens}、"
        f"输出 {report.completion_tokens} tokens"
    )
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
    if not args.dry_run and sent is None:
        print("未配置告警，日报未发送")
    return 0 if usage_ok and (args.dry_run or sent is True) else 1


def _cmd_reclassify(_: argparse.Namespace) -> int:
    """按当前的标题规则重算每份文件的类型（rule_kind）、项目类别（rule_category）和筛选标签
    （rule_supports、rule_industries），并去掉标题开头从列表页带进来的装饰符号（crawler/titles.py），
    只写有变化的。规则改了之后要跑一次；admin 服务每次启动时在迁移之后自动跑。"""
    from collections import defaultdict

    from sqlalchemy import update

    from youju.categories import doc_category
    from youju.classify import doc_kind
    from youju.crawler.titles import clean_title
    from youju.title_tags import INDUSTRY_RULES, SUPPORT_RULES

    with session_factory()() as session:
        rows = session.execute(
            select(
                Document.id,
                Document.title,
                Document.rule_kind,
                Document.rule_category,
                Document.rule_supports,
                Document.rule_industries,
            )
        ).all()
        changed: dict[tuple[str, str, str, str], list[int]] = defaultdict(list)
        cleaned_titles: dict[int, str] = {}
        for doc_id, title, *stored in rows:
            cleaned = clean_title(title)
            if cleaned != title:
                cleaned_titles[doc_id] = cleaned
            new = (
                doc_kind(cleaned),
                doc_category(cleaned),
                SUPPORT_RULES.stored(cleaned),
                INDUSTRY_RULES.stored(cleaned),
            )
            if new != tuple(stored):
                changed[new].append(doc_id)
        for (kind, category, supports, industries), ids in changed.items():
            for start in range(0, len(ids), 1000):
                session.execute(
                    update(Document)
                    .where(Document.id.in_(ids[start : start + 1000]))
                    .values(
                        rule_kind=kind,
                        rule_category=category,
                        rule_supports=supports,
                        rule_industries=industries,
                    )
                )
        # 标题清理与标签批量重算分开，避免每种标题都单发一次标签 UPDATE。
        for doc_id, title in cleaned_titles.items():
            session.execute(update(Document).where(Document.id == doc_id).values(title=title))
        updated_ids = set(cleaned_titles) | {i for ids in changed.values() for i in ids}
        session.commit()
    print(
        f"按当前规则重算文件类型、项目类别和筛选标签：共 {len(rows)} 份，更新 {len(updated_ids)} 份"
    )
    return 0


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
    print(f"已恢复 {sum(counts.values())} 条记录，各表条数、外键和内容都和备份一致：")
    for name, count in counts.items():
        print(f"  {name}: {count}")
    return 0


def _cmd_backup_verify(args: argparse.Namespace) -> int:
    from youju.backup import BackupError, download_latest, verify
    from youju.db import create_mysql_engine

    settings = get_settings()
    if not args.file and not settings.cloud_env:
        print("没有设置 YOUJU_CLOUD_ENV（云托管环境 ID），无法下载备份")
        return 1
    engine = create_mysql_engine(settings.database_url)
    try:
        if args.file:
            with args.file.open("rb") as src:
                counts = verify(engine, src)
            path = str(args.file)
        else:
            import httpx

            with tempfile.TemporaryDirectory() as tmp, httpx.Client(trust_env=False) as client:
                local = Path(tmp) / "backup.jsonl.gz"
                path = download_latest(client, settings.cloud_env, local)
                with local.open("rb") as src:
                    counts = verify(engine, src)
    except (BackupError, OSError) as exc:
        print(f"备份校验失败：{exc}")
        return 1
    finally:
        engine.dispose()
    print(f"备份校验通过：{path}，共 {sum(counts.values())} 条记录")
    for name, count in counts.items():
        print(f"  {name}: {count}")
    return 0


def _cmd_check_links(args: argparse.Namespace) -> int:
    from youju.link_check import check_links

    with session_factory()() as session:
        counts = check_links(session, limit=args.limit)
    print(
        f"检查原网页 {counts['checked']} 份：能打开 {counts['ok']}，"
        f"第一次打不开待确认 {counts['suspect']}，确认失效 {counts['gone']}，"
        f"暂时判断不了 {counts['unknown']}"
    )
    return 0


def _cmd_archive_logs(args: argparse.Namespace) -> int:
    from youju.access_log import run_archive
    from youju.backup import BackupError
    from youju.db import create_mysql_engine

    env = get_settings().cloud_env
    if not args.output and not env:
        print("没有设置 YOUJU_CLOUD_ENV（云托管环境 ID），无法上传访问日志归档")
        return 1
    engine = create_mysql_engine(get_settings().database_url)
    try:
        report = run_archive(engine, env, output=args.output)
    except BackupError as exc:
        print(f"访问日志归档失败：{exc}")
        return 1
    finally:
        engine.dispose()
    days = "，".join(f"{day:%m-%d} {rows} 条" for day, rows in report.days.items())
    print(
        f"访问日志归档：{days or '没有要归档的'}（共 {report.bytes} 字节）；"
        f"删掉超过 190 天的暂存 {report.expired} 条、过期归档 {report.pruned} 份"
    )
    return 0


def _cmd_log_uid(args: argparse.Namespace) -> int:
    from youju.access_log import log_uid

    print(log_uid(args.openid))
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


def _cmd_compare_lists(args: argparse.Namespace) -> int:
    from youju.compare import compare_lists, export_compare_excel, read_customer_file

    try:
        values = read_customer_file(args.file)
        with session_factory()() as session:
            report = compare_lists(session, values)
        rows = export_compare_excel(report, args.output)
    except ValueError as exc:
        print(f"名单比对失败：{exc}")
        return 1
    print(f"名单比对完成：客户 {len(report.rows)} 行，明细 {rows} 行，已写入 {args.output}")
    if report.removed:
        print("已去掉：" + "、".join(f"{r.reason} {r.value}" for r in report.removed[:10]))
    return 0


def _cmd_quality_sample(args: argparse.Namespace) -> int:
    from youju.quality import sample_csv

    with session_factory()() as session:
        data, rows = sample_csv(session, args.kind, n=args.n, seed=args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(data)
    label = "通知" if args.kind == "notices" else "名单比对"
    print(f"抽检表（{label}）已写入 {args.out}：{rows} 行（n={args.n}，seed={args.seed}）")
    return 0


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("至少 1")
    return number


def _non_negative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("不能小于 0")
    return number


def _cmd_track_clients(args: argparse.Namespace) -> int:
    from youju.track import export_track_excel, read_customer_profiles, track_clients

    try:
        profiles = read_customer_profiles(args.file)
        with session_factory()() as session:
            report = track_clients(session, profiles)
        rows = export_track_excel(report, args.output)
    except ValueError as exc:
        print(f"客户跟进表失败：{exc}")
        return 1
    invalid = sum(client.profile.invalid_province for client in report.clients)
    print(f"客户跟进表完成：客户 {len(report.clients)} 行，明细 {rows} 行，已写入 {args.output}")
    if invalid:
        print(f"省份不支持、未查询：{invalid} 行")
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


def _cmd_cache_attachments(args: argparse.Namespace) -> int:
    """附件副本（youju/attachments.py）：申报通知近 180 天、政策近 90 天的附件
    存到对象存储 attachments/，
    清理过期的。原网站下载失败是常事，记在表里、按时重试，不算任务失败；
    对象存储出错才按失败退出（job.sh 提醒）。"""
    from youju.attachments import cache_attachments

    env = get_settings().cloud_env
    if not env:
        print("没有设置 YOUJU_CLOUD_ENV（云托管环境 ID），无法存附件副本")
        return 1
    with session_factory()() as session:
        report = cache_attachments(session, env, limit=args.limit, retry_failed=args.retry_failed)
    if report.skipped:
        print("附件副本：另一个附件任务（上一轮或手动重查）正在运行，本轮跳过")
        return 0
    failed = "，".join(f"{code} {n}" for code, n in report.failed.most_common()) or "无"
    print(
        f"附件副本：新登记 {report.added}，本轮下载 {report.checked}（{report.bytes} 字节），"
        f"新存或更新 {report.stored}，未变 {report.unchanged}，删掉 {report.removed}，"
        f"到期待下轮 {report.deferred}；没存成的原因：{failed}"
    )
    recheck = report.recheck
    if recheck and (recheck.checked or recheck.unreadable):
        dropped = "，".join(f"{code} {n}" for code, n in recheck.dropped.most_common()) or "无"
        print(
            f"旧规则版本的副本按新规则重查 {recheck.checked} 个：恢复提供 {recheck.kept}，"
            f"不再提供 {sum(recheck.dropped.values())}（{dropped}）；"
            f"读不到、下轮再查 {recheck.unreadable}"
        )
    if report.storage_error:
        print(report.storage_error)
        return 1
    return 0


def _cmd_recheck_attachments(args: argparse.Namespace) -> int:
    """按现在的规则（个人信息、图片等）重新检查对象存储里已存的附件副本，不合格的改成不提供副本、
    从对象存储删掉。规则收紧后跑一次；--dry-run 只报告不改。"""
    from youju.attachments import recheck_copies

    env = get_settings().cloud_env
    if not env:
        print("没有设置 YOUJU_CLOUD_ENV（云托管环境 ID），无法读取附件副本")
        return 1
    with session_factory()() as session:
        report = recheck_copies(session, env, dry_run=args.dry_run)
    if report.skipped:
        print("附件副本的定时任务正在运行，这次没有重查，请稍后再跑")
        return 1
    dropped = "，".join(f"{code} {n}" for code, n in report.dropped.most_common()) or "无"
    action = "（只检查，没有改动）" if args.dry_run else f"，从对象存储删掉 {report.removed} 个"
    print(
        f"附件副本重新检查：检查 {report.checked} 个，合格 {report.kept}；"
        f"不合格 {sum(report.dropped.values())} 个（{dropped}），"
        f"涉及 {report.rows} 条附件改为不提供副本{action}；读不到的 {report.unreadable} 个"
    )
    if report.storage_error:
        print(report.storage_error)
    return 1 if report.storage_error or report.unreadable else 0


def _cmd_settle_missing_copies(args: argparse.Namespace) -> int:
    """删不掉的附件副本凭对象存储清单核对（见 attachments.settle_missing_copies）。只输出个数、
    留档路径和固定说明的 JSON，不输出文件 ID。"""
    from dataclasses import asdict

    from youju.attachments import parse_listing, settle_missing_copies

    env = get_settings().cloud_env
    if not env:
        print("没有设置 YOUJU_CLOUD_ENV（云托管环境 ID），无法核对附件副本")
        return 1
    now = datetime.now(UTC)
    try:
        listing = parse_listing(sys.stdin.read(), now)
    except ValueError as exc:
        print(json.dumps({"error": f"清单不能用：{exc}"}, ensure_ascii=False))
        return 1
    with session_factory()() as session:
        report = settle_missing_copies(session, env, listing, now=now, apply=args.apply)
    print(json.dumps(asdict(report), ensure_ascii=False))
    if report.skipped:
        return 75
    return 1 if report.error else 0


def _cmd_advisor_orphans(args: argparse.Namespace) -> int:
    """批量工具结果文件巡检（见 youju/advisor_orphans.py）：对象存储里有、但没有任何记录指向的
    结果文件。默认只报个数，加 --delete 才删；只输出个数和文件编号前缀。"""
    from youju import advisor_orphans
    from youju.backup import BackupError

    settings = get_settings()
    if not settings.cloud_env:
        print("没有设置 YOUJU_CLOUD_ENV（云托管环境 ID），无法巡检结果文件")
        return 1
    try:
        with session_factory()() as session:
            report = advisor_orphans.scan(session, settings, delete=args.delete)
    except BackupError as exc:
        print(f"结果文件巡检失败，没有删除任何文件：{exc}")
        return 1
    print(report.text())
    if args.alert and report.orphans:
        from youju.alerts import send_alert

        if send_alert(report.alert_text()) is not True:
            print("提醒没有发出去")
            return 1
    return 1 if report.failed or report.blocked else 0


def _cmd_ai_eligibility(args: argparse.Namespace) -> int:
    """每份文件能不能做 AI 解读（D-075），存进 ai_eligibility 给列表行用。默认只重算最近 30 分钟
    有变化的和还没算过的（admin 每 10 分钟跑）；--all（命令 ai-eligibility-rebuild）全部重算
    （每天夜里跑；手动改过来源级别后也跑一次）。和 AI 开没开无关，开关关着也照算。"""
    from youju.ai import eligibility

    full = getattr(args, "all", False)
    wait = eligibility.FULL_WAIT if full else 0
    # 同一时间只让一个重算写：增量碰上就跳过这次；全量等不到算失败，job.sh 会提醒
    with session_factory()() as session, eligibility.writer_lock(session, wait) as got:
        if not got:
            if full:
                print("AI 解读可用性：另一个重算一直没跑完，这次全量没算")
                return 1
            print("AI 解读可用性：另一个重算还在跑，这次跳过")
            return 0
        stamp = datetime.now(UTC)
        if full:
            report = eligibility.refresh_all(session, stamp)
        else:
            report = eligibility.refresh_changed(session, stamp)
    print(f"AI 解读可用性：重算 {report['checked']} 份，结果变了 {report['changed']} 份")
    return 0


def _cmd_ai_heartbeat(_: argparse.Namespace) -> int:
    """AI 设置心跳（D-075）：admin 每分钟写一行，api 只在这里说能处理、两边设置一致时才收 AI 任务；
    两边设置对不上、api 心跳没来时提醒。单独一条定时任务，不和 advisor-run 抢锁。"""
    from youju.ai import heartbeat

    settings = get_settings()
    stamp = datetime.now(UTC)
    with session_factory()() as session:
        row = heartbeat.beat(session, settings, "admin", stamp)
        heartbeat.check_and_alert(session, settings, stamp)
        print(f"AI 设置心跳：admin {'能' if row.ai_enabled else '不能'}处理 AI 任务")
    return 0


def _cmd_advisor_run(_: argparse.Namespace) -> int:
    from youju.advisor_tasks import run_advisor_tasks

    # admin 没开 AI 时退回的任务由 ai-heartbeat 每分钟按任务表数出来提醒（D-075），这里不发
    with session_factory()() as session:
        report = run_advisor_tasks(session)
    print("工具任务：" + "，".join(f"{name} {count}" for name, count in report.items()))
    return 0


def _cmd_rewards_reconcile(_: argparse.Namespace) -> int:
    from youju import rewards

    with session_factory()() as session:
        report = rewards.reconcile(session)
    print("奖励补发：" + "，".join(f"{name} {count}" for name, count in report.items()))
    return 1 if report["failed"] else 0


def _reward_revoke_reason(value: str) -> str:
    reason = value.strip()
    if not 6 <= len(reason) <= 100:
        raise argparse.ArgumentTypeError("收回原因须为 6–100 字")
    return reason


def _cmd_rewards_revoke(args: argparse.Namespace) -> int:
    from youju import payments, rewards

    stamp = datetime.now(UTC)
    with session_factory()() as session:
        try:
            reward, ticket = rewards.lock_reward(session, args.reward_id)
        except ValueError as exc:
            print(str(exc))
            return 1
        unused = (
            payments.unused_pass_time(ticket, stamp)
            if reward.user_id is not None and not rewards.is_revoked(reward)
            else timedelta(0)
        )
        print(f"奖励 #{reward.id}：{reward.kind}（{rewards.LABELS[reward.kind]}）")
        print(f"发放天数：{reward.granted_days}；发放时间：{reward.granted_at.isoformat()}")
        print(f"用户内部编号：{reward.user_id if reward.user_id is not None else '已注销'}")
        print(
            f"使用期：{ticket.starts_at.isoformat()} 至 {ticket.ends_at.isoformat()}"
            if ticket is not None
            else "使用期：无"
        )
        print(f"将要收回时间：{unused}（{unused.total_seconds():g} 秒）")
        if args.dry_run:
            print("演练完成，未写入")
            session.rollback()
        else:
            result = rewards.revoke(session, args.reward_id, args.reason, stamp)
            session.commit()
            print(result)
    return 0


def _refund_reason(value: str) -> str:
    reason = value.strip()
    if not 6 <= len(reason) <= 100:
        raise argparse.ArgumentTypeError("退款原因须为 6–100 字")
    return reason


def _refund_one(session, order_id: int, args, stamp, client) -> str:
    """处理一笔：打印要退多少，演练时不写入；返回 refunded（已发起）/ skipped / missing。"""
    from youju import payments, rewards

    order = payments.lock_order(session, order_id)
    if order is None:
        session.rollback()
        print(f"订单 #{order_id}：没有找到")
        return "missing"
    problem = payments.staff_refund_problem(session, order)
    ticket = payments.order_pass(session, order, lock=True)
    extra = args.extra_days or 0
    fee = order.price if args.full else payments.remaining_fee(order, ticket, stamp, extra)
    days = order.days if args.full else payments.remaining_days(order, ticket, stamp, extra)
    period = (
        f"{ticket.starts_at.isoformat()} 至 {ticket.ends_at.isoformat()}"
        + ("（已收回）" if ticket.revoked_at else "")
        if ticket
        else "无"
    )
    print(
        f"订单 #{order.id}：{payments.pass_name(order.days)}，{order.price / 100:.2f} 元，"
        f"付款 {order.paid_time.isoformat() if order.paid_time else '未确认'}；使用期 {period}"
    )
    gifts = [
        f"{gift.granted_days} 天{'（已收回）' if rewards.is_revoked(gift) else ''}"
        for gift, _ in payments.order_gifts(session, order)
        if gift.ref_type == payments.RENEWAL_REF
    ]
    if problem is None and fee <= 0:
        problem = "使用期已经用完，没有可退的金额"
    if problem is not None:
        session.rollback()
        print(f"  不退：{problem}")
        return "skipped"
    print(
        f"  {'全额退' if args.full else f'按剩余 {days} 天退'}：{fee / 100:.2f} 元"
        + (f"（含此前没补上的 {extra} 天）" if extra and not args.full else "")
        + "；"
        f"收回这一笔的使用期{'和随它送的续买 ' + '、'.join(gifts) if gifts else ''}"
    )
    # 退款不等冲红（D-074）：已开过发票的照常退，退完再按退款金额开红字发票
    reverse = payments.invoice_issued(session, order)
    if reverse:
        print("  这笔已开过发票：退款后请按退款金额开红字发票（管理台开票页选“要冲红”能看到）")
    if args.dry_run:
        session.rollback()
        return "refunded"
    payments.begin_refund(
        session, order, stamp, fee=fee, source=payments.STAFF_REFUND, note=args.reason
    )
    payments.start_refund(session, order, client, stamp)
    session.expire_all()
    order = session.get(type(order), order_id)
    print(
        f"  已发起：订单状态 {payments.STATUS_LABELS[order.status]}"
        + (f"（{order.error}）" if order.error else "")
    )
    return "refunded"


def _cmd_pay_refund(args: argparse.Namespace) -> int:
    """负责人按规则发起退款（D-070）：系统故障、停止服务按剩余天数退（--remaining），
    重复扣款、整段没法用的全额退（--full）。只退安卓、鸿蒙、电脑版微信付款的；苹果手机的请用户向
    App Store 申请。先加 --dry-run 看清楚要退哪些、退多少。"""
    import httpx

    from youju.config import get_settings
    from youju.models import PayOrder, WorkbenchPass
    from youju.xpay import XPayClient

    if args.all_active and (args.full or args.extra_days):
        # 停止服务按剩余天数退（已开过发票的照样退，退完再冲红）；
        # 另算天数的要一笔一笔用 --order-id 退
        print("--all-active 只能和 --remaining 一起用，不能加 --extra-days")
        return 2
    if args.full and args.extra_days:
        print("--extra-days 只能和 --remaining 一起用（全额退不用另算天数）")
        return 2
    settings = get_settings()
    stamp = datetime.now(UTC)
    with session_factory()() as session, httpx.Client(trust_env=False) as http:
        client = XPayClient(settings, http)
        if args.order_id is not None:
            ids = [args.order_id]
        else:
            # 停止服务：使用期还没用完、没退过款的订单（含提前续买、还没开始的）
            ids = list(
                session.scalars(
                    select(PayOrder.id)
                    .join(WorkbenchPass, WorkbenchPass.order_id == PayOrder.id)
                    .where(
                        PayOrder.status == "completed",
                        PayOrder.refund_status.is_(None),
                        WorkbenchPass.revoked_at.is_(None),
                        WorkbenchPass.ends_at > stamp,
                    )
                    .order_by(PayOrder.id)
                )
            )
            session.rollback()
            print(f"使用期还没用完的订单 {len(ids)} 笔")
        report = {"refunded": 0, "skipped": 0, "missing": 0}
        for order_id in ids:
            report[_refund_one(session, order_id, args, stamp, client)] += 1
    print(
        ("演练完成，未写入：可退 " if args.dry_run else "已发起退款 ")
        + f"{report['refunded']} 笔，不退 {report['skipped']} 笔"
        + (f"，没找到 {report['missing']} 笔" if report["missing"] else "")
    )
    if report["skipped"] and not args.dry_run:
        print("不退的订单请按上面的原因处理（苹果手机付款的请用户在 App Store 申请）")
    return 1 if report["missing"] else 0


def _cmd_pay(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime, timedelta

    import httpx

    from youju import payments
    from youju.config import get_settings
    from youju.xpay import XPayClient

    settings = get_settings()
    stamp = datetime.now(UTC)
    day = payments.advisor_tasks.usage_day(stamp) - timedelta(days=1)
    with session_factory()() as session:
        # 支付开关只停新下单：关闭后仍处理未完结订单；没有待办时只查本地库，不访问微信。
        if args.command == "pay-reconcile":
            if not payments.should_reconcile(session, settings, day):
                return 0
        elif not payments.has_work(session, settings):
            return 0
        with httpx.Client(trust_env=False) as http:
            client = XPayClient(settings, http)
            if args.command == "pay-reconcile":
                payments.download_reconcile(session, day, client, stamp)
            else:
                payments.run_payments(session, settings=settings, client=client, stamp=stamp)
    return 0


def _cmd_alert(args: argparse.Namespace) -> int:
    from youju.alerts import send_alert

    sent = send_alert(args.text)
    if sent is None:
        print("未配置告警，消息未发送")
    return 0 if sent is True else 1


def _cmd_alert_probe(args: argparse.Namespace) -> int:
    from youju.alerts import probe_alert

    try:
        result = probe_alert(send=args.send, probe_id=args.probe_id)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    if not result["configured"]:
        return 2
    return 1 if result["state"] == "failed" else 0


def _cmd_check_sources(args: argparse.Namespace) -> int:
    from youju.alerts import MAX_ALERT_BYTES, send_alert
    from youju.source_health import check_sources, failing_sources, is_streak_milestone

    with session_factory()() as session:
        health = check_sources(session)
        # 成功率按比例算，75 个栏目坏 3 个也达标：单个栏目一直坏着从来不提醒（湖南工信厅两个栏目
        # 10/10 起天天识别不到条目，没人发现）。早上那一轮（--streaks）另外看哪些栏目今天正好
        # 连续没抓到第 3、7、14、30 天（之后每满 30 天），各提醒一次；日报每天写"上次成功几号"。
        streaks = (
            [item for item in failing_sources(session) if is_streak_milestone(item.days)]
            if args.streaks
            else []
        )
    # 连续天数单独发：和成功率提醒拼在一起时，失败栏目多了会被群消息的字节上限截掉；
    # 一条放不下就分几条，每个到节点的栏目都列出来（不写"另有几个"，免得后面的永远轮不到）
    streak_sent = None
    if streaks:
        for text in _streak_messages(streaks, MAX_ALERT_BYTES - len("【有据】".encode())):
            log.warning("%s", text)
            result = send_alert(text)
            # 有一条没发出去就算没发出去；都没配告警地址时是 None
            streak_sent = False if result is False or streak_sent is False else result
    if health.passed:
        log.info("今天栏目检查达标：%s/%s（%.1f%%）", health.successful, health.total, health.rate)
        # 发送失败（没配告警地址不算）记为任务失败：每个节点只提醒这一次，
        # 没发出去要在任务记录里看得到
        return 1 if streak_sent is False else 0
    message = health.alert_text()
    log.warning("%s", message)
    sent = send_alert(message)
    # cron 包装器用专用码识别“详细提醒已发”，避免再发一条泛化的任务失败提醒；
    # 连续天数那条没发出去就不算"都发了"
    return 20 if args.job and sent is True and streak_sent is not False else 1


def _streak_messages(streaks: list, budget: int) -> list[str]:
    """按群消息的字节上限把到节点的栏目分成几条，每条都带开头那句；单个栏目太长时截断它自己。"""
    head = "这些栏目连续好几天没抓到，请到管理台“来源管理”看看网站是不是改版了："
    messages: list[str] = []
    lines = [head]
    for item in streaks:
        line = f"第 {item.days} 天：{item.text()}"
        room = budget - len(head.encode()) - 1
        if len(line.encode()) > room:
            line = line.encode()[: room - len("…".encode())].decode("utf-8", "ignore") + "…"
        if len("\n".join([*lines, line]).encode()) > budget:
            messages.append("\n".join(lines))
            lines = [head]
        lines.append(line)
    messages.append("\n".join(lines))
    return messages


def _cmd_job_start(args: argparse.Namespace) -> int:
    from youju.jobs import start_run

    run_id = start_run(args.job)
    if run_id is not None:
        print(run_id)
    return 0


def _cmd_job_finish(args: argparse.Namespace) -> int:
    from youju.jobs import finish_run

    finish_run(args.id, args.code)
    return 0


def _cmd_weekly_digest(args: argparse.Namespace) -> int:
    from youju import weekly
    from youju.models import DigestProfile

    stamp = weekly.now()
    reminder = args.kind == "reminder"
    weekday = stamp.astimezone(weekly.BEIJING).isoweekday()
    try:
        weekly.interval(args.kind, args.week, stamp)
        if args.scheduled and reminder and weekday < weekly.REMINDER_WEEKDAY:
            print("临期提醒每周四生成、周五到周日补跑，今天不安排")
            return 0
        with session_factory()() as session:
            stmt = select(DigestProfile).where(DigestProfile.enabled.is_(True))
            if args.profile:
                stmt = select(DigestProfile).where(DigestProfile.name == args.profile)
            if args.scheduled and not reminder:
                stmt = stmt.where(DigestProfile.weekday == weekday)
            profiles = session.scalars(stmt.order_by(DigestProfile.id)).all()
            if args.profile and not profiles:
                print("没有找到周报方案")
                return 1
            for profile in profiles:
                # 临期提醒的定时补跑只补本周还没有的，已生成（含已发布）的不覆盖。
                result = weekly.generate(
                    session,
                    profile.id,
                    args.week,
                    stamp,
                    args.kind,
                    only_missing=args.scheduled and reminder,
                )
                print(
                    f"方案 #{profile.id}：{result.reason}"
                    + (f"（{weekly.KINDS[args.kind]} #{result.issue.id}）" if result.issue else "")
                )
            if not profiles:
                print("没有需要生成的启用方案")
    except ValueError as exc:
        print(str(exc))
        return 1
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
    p.add_argument("--since", type=date.fromisoformat, help="只处理此发布日期以来的文件")
    p.add_argument(
        "--notices-only", action="store_true", help="只处理标题含申报/征集/推荐/遴选/认定的文件"
    )
    p.set_defaults(func=_cmd_fetch_details)

    p = sub.add_parser(
        "backfill-titles",
        help="用详情页的完整标题补全列表页截断的历史标题（默认只预览，加 --apply 才写入）",
    )
    p.add_argument("--apply", action="store_true", help="写入；不加只预览")
    p.add_argument("--limit", type=int, default=500, help="最多处理多少份，默认 500")
    p.add_argument("--source-id", type=int, help="只处理指定来源")
    p.add_argument("--since", type=date.fromisoformat, help="只处理此发布日期以来的文件")
    p.add_argument("--after-id", type=int, help="只处理编号大于此值的文件（分批时接着上一批）")
    p.add_argument("--workers", type=int, help="同时抓取的网站数")
    p.set_defaults(func=_cmd_backfill_titles)

    p = sub.add_parser(
        "rescan-list-details",
        help="一次性：重抓还没读出企业名的名单详情，补上按新规则才认得出的附件"
        "（默认只预览，加 --apply 才写入）",
    )
    p.add_argument("--apply", action="store_true", help="写入（只追加新认出的附件）；不加只预览")
    p.add_argument("--limit", type=int, default=200, help="最多检查多少份，默认 200")
    p.add_argument(
        "--host-suffix",
        action="append",
        help="只查这些网站后缀（如 .henan.gov.cn，可重复）；默认是附件文件服务器服务的省份",
    )
    p.set_defaults(func=_cmd_rescan_list_details)

    p = sub.add_parser(
        "refetch-bodies",
        help="一次性：重新打开原网页，按现在的规则重取正文，换掉混进网页导航等字样的正文"
        "（默认只预览，加 --apply 才写入）",
    )
    p.add_argument(
        "--host", action="append", help="只查这个网站（主机名，如 kjt.hubei.gov.cn，可重复）"
    )
    p.add_argument("--source-id", type=int, action="append", help="只查这个来源（可重复）")
    p.add_argument("--apply", action="store_true", help="写入（只换正文和抓取时间）；不加只预览")
    p.add_argument(
        "--include-changed",
        action="store_true",
        help="不只是去掉开头结尾网页字样的（原网页改过、以前没取到正文）也换；先看预览再加",
    )
    p.add_argument("--limit", type=int, default=200, help="最多检查多少份，默认 200")
    p.add_argument("--after-id", type=int, help="只查编号大于此值的（分批时接着上一批）")
    p.add_argument("--workers", type=int, help="同时抓取的网站数")
    p.add_argument(
        "--survey",
        action="store_true",
        help="只读、不联网：按来源数正文带网页字样（当前位置、扫一扫、打印等）的份数，挑要重抓的网站",
    )
    p.set_defaults(func=_cmd_refetch_bodies)

    p = sub.add_parser("extract-deadlines", help="从已抓取正文生成待核对截止候选")
    p.add_argument("--since", type=date.fromisoformat, help="只处理此发布日期以来的文件")
    p.set_defaults(func=_cmd_extract_deadlines)
    p = sub.add_parser("deadline-excerpts", help="只读检查截止摘录来源与拦下原因")
    p.add_argument("--sample", type=int, default=50)
    p.set_defaults(func=_cmd_deadline_excerpts)

    sub.add_parser(
        "repost-groups",
        help="把同一份部里通知的各地版本归成转发组（待人工确认；所有组必须人工确认）",
    ).set_defaults(func=_cmd_repost_groups)

    p = sub.add_parser("extract-fields", help="从申报通知正文生成支持方式和申报对象候选")
    p.add_argument("--since", type=date.fromisoformat, help="只处理此发布日期以来的文件")
    p.set_defaults(func=_cmd_extract_fields)

    p = sub.add_parser("ai-check", help="AI 做类型判断和截止抽取，和规则互相印证（没配密钥时跳过）")
    p.add_argument("--limit", type=int, default=300, help="本轮最多检查多少份文件")
    p.add_argument(
        "--due-first",
        type=_non_negative,
        metavar="DAYS",
        help="先补跑 DAYS 天内截止的待核对候选所在的文件（AI 还没抽过截止的），再查其他文件；"
        "合计不超过 --limit。只写 AI 记录，不改候选的核对状态",
    )
    p.set_defaults(func=_cmd_ai_check)

    p = sub.add_parser(
        "extract-conditions",
        help="后台模型从申报通知正文抽申报条件（每条带原文引用），送核对台；没配密钥时跳过",
    )
    p.add_argument("--limit", type=int, default=60, help="本轮最多抽取多少份通知")
    p.add_argument(
        "--due-days",
        type=_non_negative,
        default=30,
        metavar="DAYS",
        help="DAYS 天内截止的通知排在前面先抽（默认 30）",
    )
    p.set_defaults(func=_cmd_extract_conditions)

    p = sub.add_parser(
        "audit-deadlines",
        help="质检已发布的截止日期：规则回查全部，配了 AI 时再抽查一部分；存疑的提醒到群里",
    )
    p.add_argument("--sample", type=int, default=20, help="AI 本次最多抽查多少份文件")
    p.set_defaults(func=_cmd_audit_deadlines)

    sub.add_parser(
        "reclassify",
        help="按当前标题规则重算类型、项目类别和筛选标签（规则改了之后跑；admin 启动时自动跑）",
    ).set_defaults(func=_cmd_reclassify)

    p = sub.add_parser("backup", help="备份数据库：导出整库，上传到云托管对象存储，保留 30 天")
    p.add_argument("--output", type=Path, help="只导出到本机这个文件，不上传")
    p.set_defaults(func=_cmd_backup)

    p = sub.add_parser(
        "check-links", help="检查近 90 天文件的原网页还能不能打开（失效的在小程序里提示）"
    )
    p.add_argument("--limit", type=int, default=400, help="这次最多检查几份")
    p.set_defaults(func=_cmd_check_links)

    p = sub.add_parser("backup-verify", help="只读校验最新备份的完整性和迁移版本")
    p.add_argument("--file", type=Path, help="校验本机备份文件，不从对象存储下载")
    p.set_defaults(func=_cmd_backup_verify)

    p = sub.add_parser(
        "restore", help="把备份文件恢复到当前数据库（要已迁移到同一版本、而且是空的）"
    )
    p.add_argument("file", type=Path, help="备份文件（.jsonl.gz）")
    p.set_defaults(func=_cmd_restore)

    p = sub.add_parser(
        "archive-logs", help="把之前每天的访问日志归档到云托管对象存储，保留 190 天，删掉过期的"
    )
    p.add_argument("--output", type=Path, help="只写到本机这个目录，不上传")
    p.set_defaults(func=_cmd_archive_logs)

    p = sub.add_parser(
        "log-uid", help="算出某个 openid 在访问日志里的用户编号（查某个用户的日志用）"
    )
    p.add_argument("openid")
    p.set_defaults(func=_cmd_log_uid)

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

    p = sub.add_parser("compare-lists", help="把客户名单与公开名单数据逐家比对并导出 Excel")
    p.add_argument("file", type=Path, help="客户名单文件（.xlsx 或 .csv）")
    p.add_argument("-o", "--output", type=Path, required=True, help="输出 Excel 文件")
    p.set_defaults(func=_cmd_compare_lists)

    p = sub.add_parser(
        "quality-sample", help="生成数据质量抽检表（CSV）：抽样并先做程序核对，其余由人工看"
    )
    p.add_argument("--kind", choices=("notices", "compare"), required=True, help="通知或名单比对")
    p.add_argument("--n", type=_positive, default=200, help="抽多少条，默认 200")
    p.add_argument("--seed", type=int, default=1, help="抽样种子，同一种子同一份结果，默认 1")
    p.add_argument("--out", type=Path, required=True, help="输出 CSV 文件")
    p.set_defaults(func=_cmd_quality_sample)

    p = sub.add_parser("track-clients", help="按客户画像生成多客户在报项目跟进表")
    p.add_argument("file", type=Path, help="客户画像文件（.xlsx 或 .csv）")
    p.add_argument("-o", "--output", type=Path, required=True, help="输出 Excel 文件")
    p.set_defaults(func=_cmd_track_clients)

    sub.add_parser("report", help="打印来源健康和条目统计").set_defaults(func=_cmd_report)
    p = sub.add_parser("weekly-digest", help="生成固定模板政策周报或临期提醒草稿，供人工复制发布")
    p.add_argument("--profile", help="方案名称；未指定时生成全部启用方案")
    p.add_argument(
        "--kind",
        choices=("weekly", "reminder"),
        default="weekly",
        help="weekly：周报（默认）；reminder：临期提醒，列出未来 7 天内截止且已核对的申报通知",
    )
    p.add_argument(
        "--week",
        type=date.fromisoformat,
        help="统计周的周一，例如 2026-09-28；临期提醒只能是本周周一，留空即本周",
    )
    p.add_argument(
        "--scheduled",
        action="store_true",
        help="仅生成今天安排的方案（供定时任务用）；临期提醒周四至周日运行，本周已生成的跳过",
    )
    p.set_defaults(func=_cmd_weekly_digest)
    p = sub.add_parser(
        "daily-report",
        help="运维日报：运行情况、待办和 AI 用量发到群里，配了 AI 时附上建议先核对的通知",
    )
    p.add_argument("--dry-run", action="store_true", help="只打印，不发到群里")
    p.set_defaults(func=_cmd_daily_report)
    sub.add_parser(
        "notify", help="发送一次性订阅提醒；未设置 YOUJU_SEND_WX_MESSAGES=true 时只演练"
    ).set_defaults(func=_cmd_notify)
    sub.add_parser(
        "cleanup", help="按保存期限删除埋点、纠错和已用完的订阅授权；旧的奖励资格凭据一律删除"
    ).set_defaults(func=_cmd_cleanup)
    sub.add_parser("rewards-reconcile", help="补发漏掉的截止线索奖励").set_defaults(
        func=_cmd_rewards_reconcile
    )
    p = sub.add_parser("rewards-revoke", help="收回一笔奖励的未用时间（开关关闭后仍可运行）")
    p.add_argument("--reward-id", type=int, required=True, help="奖励流水编号")
    p.add_argument(
        "--reason",
        type=_reward_revoke_reason,
        required=True,
        help="收回原因，6–100 字；用户能在自己的奖励记录里看到这句话",
    )
    p.add_argument("--dry-run", action="store_true", help="只打印，不写入")
    p.set_defaults(func=_cmd_rewards_revoke)
    sub.add_parser("pay-run", help="支付补单、退款及发货通知（关闭后处理未完结订单）").set_defaults(
        func=_cmd_pay
    )
    sub.add_parser(
        "pay-reconcile", help="前一天支付日账单对账（关闭后只在那天有本地订单时对账）"
    ).set_defaults(func=_cmd_pay)
    p = sub.add_parser(
        "pay-refund",
        help="负责人按规则退款：系统故障或停止服务按剩余天数退，重复扣款全额退（开关关闭后仍可运行）",
    )
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("--order-id", type=int, help="订单编号（用户订单页上的订单号）")
    which.add_argument(
        "--all-active", action="store_true", help="停止服务：使用期还没用完的所有订单"
    )
    how = p.add_mutually_exclusive_group(required=True)
    how.add_argument("--remaining", action="store_true", help="按剩余天数退（不足一天按一天）")
    how.add_argument("--full", action="store_true", help="全额退")
    p.add_argument(
        "--reason", type=_refund_reason, required=True, help="退款原因，6–100 字，记在订单上"
    )
    p.add_argument(
        "--extra-days",
        type=int,
        choices=range(0, 91),
        metavar="0-90",
        help="此前因系统故障不能用、又没有延长补上的天数，核实后加进去一起退"
        "（只对 --order-id --remaining 有用）",
    )
    p.add_argument("--dry-run", action="store_true", help="只打印要退哪些、退多少，不写入")
    p.set_defaults(func=_cmd_pay_refund)
    sub.add_parser("advisor-run", help="处理顾问技能包任务（开关关闭时跳过）").set_defaults(
        func=_cmd_advisor_run
    )
    sub.add_parser(
        "ai-heartbeat", help="写 admin 的 AI 设置心跳；两个服务设置不一致时提醒（D-075）"
    ).set_defaults(func=_cmd_ai_heartbeat)
    sub.add_parser(
        "ai-eligibility",
        help="重算最近 30 分钟有变化的文件能不能做 AI 解读（D-075，列表行的按钮用）",
    ).set_defaults(func=_cmd_ai_eligibility, all=False)
    sub.add_parser(
        "ai-eligibility-rebuild",
        help="全部文件重算能不能做 AI 解读（每天夜里；手动改过来源级别后也跑一次）",
    ).set_defaults(func=_cmd_ai_eligibility, all=True)
    p = sub.add_parser(
        "cache-attachments",
        help="下载申报通知近 180 天、政策近 90 天的附件存到对象存储，供小程序直接打开；"
        "清理过期的副本",
    )
    p.add_argument("--limit", type=int, default=100, help="本轮最多下载几个（1–500，默认 100）")
    p.add_argument("--retry-failed", action="store_true", help="没存成的不等重试时间，本轮就重试")
    p.set_defaults(func=_cmd_cache_attachments)
    p = sub.add_parser(
        "recheck-attachments",
        help="按现在的规则重新检查已存的附件副本，不合格的不再提供并从对象存储删掉（规则收紧后跑一次）",
    )
    p.add_argument("--dry-run", action="store_true", help="只检查、报告，不改表、不删文件")
    p.set_defaults(func=_cmd_recheck_attachments)
    p = sub.add_parser(
        "settle-missing-copies",
        help="从标准输入读对象存储清单（scripts/attachment_listing.py 生成），核对删不掉、"
        "清单里也没有的附件副本；加 --apply 才留档并结算",
    )
    p.add_argument("--apply", action="store_true", help="照常删除，证据齐全的留档后结算")
    p.set_defaults(func=_cmd_settle_missing_copies)
    p = sub.add_parser(
        "advisor-orphans",
        help="巡检批量工具的结果文件：找出对象存储里有、但没有任何记录指向的；"
        "默认只报个数，加 --delete 才删",
    )
    p.add_argument(
        "--delete", action="store_true", help="删掉找到的文件（删不掉的记进待删表，清理会重试）"
    )
    p.add_argument(
        "--alert",
        action="store_true",
        help="找到时发一条提醒到群里（给定时任务用，可和 --delete 同用）",
    )
    p.set_defaults(func=_cmd_advisor_orphans)

    p = sub.add_parser("alert", help="发送一条定时任务文本提醒")
    p.add_argument("text", help="提醒正文")
    p.set_defaults(func=_cmd_alert)

    p = sub.add_parser("alert-probe", help="只读检查告警配置；显式--send才发受控测试消息")
    p.add_argument("--send", action="store_true", help="向已配置的接收端提交一条验收消息")
    p.add_argument("--probe-id", help="可选的验收标记（6至64位字母、数字、下划线或连字符）")
    p.set_defaults(func=_cmd_alert_probe)

    p = sub.add_parser("check-sources", help="检查今天（北京时间零点起）全部启用栏目的成功率")
    p.add_argument("--job", action="store_true", help=argparse.SUPPRESS)
    p.add_argument(
        "--streaks",
        action="store_true",
        help="另外提醒今天正好连续没抓到第 3、7、14、30 天（之后每满 30 天）的栏目（只给早轮用）",
    )
    p.set_defaults(func=_cmd_check_sources)

    p = sub.add_parser("serve", help="启动 API 服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=_cmd_serve)

    from youju.jobs import JOB_NAMES

    p = sub.add_parser("job-start", help="记录定时任务开始（记账失败不阻断任务）")
    p.add_argument("job", choices=JOB_NAMES)
    p.set_defaults(func=_cmd_job_start)
    p = sub.add_parser("job-finish", help="记录定时任务结束")
    p.add_argument("id", type=int)
    p.add_argument("code", type=int)
    p.set_defaults(func=_cmd_job_finish)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # 本项目的日志一律按 INFO 输出到容器日志：basicConfig 在已有日志处理器时什么都不做，这里单独设
    logging.getLogger("youju").setLevel(logging.INFO)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
