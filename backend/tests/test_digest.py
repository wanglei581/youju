"""运维日报（D-023）：运行情况、待办和 AI 用量发到群里；AI 只从给它的待核对清单里挑几份建议先核对。"""

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from youju import digest as digest_module
from youju.ai.client import ChatClient
from youju.api import v1
from youju.digest import build_digest, digest_text, pick_for_review
from youju.models import (
    AccessDaily,
    AdvisorTask,
    AICheck,
    AIUsage,
    AttachmentCopy,
    Document,
    DocumentDeadline,
    DocumentDetail,
    DocumentMerge,
    Feedback,
    Follow,
    ListEntry,
    Source,
    SourceCheck,
    Subscription,
    User,
)

# 北京时间 9 月 29 日 8 点 10 分
NOW = datetime(2026, 9, 29, 0, 10, tzinfo=UTC)
APPLY = "关于组织申报2026年度科技计划项目的通知"
LIST = "关于公布2026年第一批高新技术企业名单的通知"


def add_source(session, n, enabled=True, status="ok"):
    source = Source(
        level="省级",
        province="山东",
        dept="科技",
        dept_full_name=f"山东省科学技术厅{n}",
        column_name="通知公告",
        url=f"https://kjt.shandong.gov.cn/tzgg{n}/",
        enabled=enabled,
        parser_config={},
    )
    session.add(source)
    session.flush()
    if status:
        session.add(
            SourceCheck(
                source_id=source.id,
                started_at=NOW - timedelta(hours=1),
                finished_at=NOW - timedelta(hours=1),
                status=status,
            )
        )
    session.commit()
    return source


def add_doc(session, source, n, title, seen, baseline=False):
    doc = Document(
        source_id=source.id,
        url=f"https://kjt.shandong.gov.cn/tzgg/{n}.html",
        url_key=f"kjt.shandong.gov.cn/tzgg/{n}.html",
        title=title,
        publish_date=date(2026, 9, 25),
        first_seen_at=seen,
        last_seen_at=seen,
        is_baseline=baseline,
    )
    session.add(doc)
    session.commit()
    return doc


def add_deadline(session, doc, due, status="pending", created=NOW, reviewed=None, n=0):
    row = DocumentDeadline(
        document_id=doc.id,
        fingerprint=f"{doc.id}-{due}-{n}".ljust(64, "x"),
        kind="企业申报截止",
        candidate_date=due,
        evidence=f"请于{due.month}月{due.day}日前完成申报",
        confidence="高",
        status=status,
        created_at=created,
        reviewed_by="创始人" if reviewed else None,
        reviewed_at=reviewed,
        approved_date=due if status == "approved" else None,
    )
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def world(session, monkeypatch):
    """一天的运行记录：两个启用栏目（一个出错），新旧文件、候选、核对、用户和 AI 用量。"""
    monkeypatch.setattr(v1, "now", lambda: NOW.astimezone(ZoneInfo("Asia/Shanghai")))
    ok = add_source(session, 1)
    add_source(session, 2, status="http_error")
    add_source(session, 3, enabled=False, status=None)
    hour = timedelta(hours=1)
    apply_doc = add_doc(session, ok, 1, APPLY, NOW - 2 * hour)
    list_doc = add_doc(session, ok, 2, LIST, NOW - 3 * hour)
    add_doc(session, ok, 3, "关于开展2026年度企业技术中心认定工作的通知", NOW - hour, baseline=True)
    old = add_doc(session, ok, 4, "关于做好2026年度科技统计工作的通知", NOW - timedelta(days=3))
    twin = add_doc(session, ok, 5, APPLY, NOW - 2 * hour)
    session.add(
        DocumentMerge(duplicate_id=twin.id, canonical_id=apply_doc.id, active=True, match_key="t")
    )
    for doc, status, fetched in (
        (apply_doc, "ok", NOW - hour),
        (list_doc, "http_error", NOW - hour),
        (old, "ok", NOW - timedelta(days=3)),
    ):
        session.add(
            DocumentDetail(document_id=doc.id, body_text="正文", fetched_at=fetched, status=status)
        )
    session.commit()
    add_deadline(session, apply_doc, date(2026, 10, 10), created=NOW - hour)
    # 同一通知另一个网址上的相同候选：核对台只列一条，日报也只算一条
    add_deadline(session, twin, date(2026, 10, 10), created=NOW - hour)
    add_deadline(session, list_doc, date(2026, 12, 20), created=NOW - hour)
    long_ago = NOW - timedelta(days=3)
    approved = add_deadline(
        session, old, date(2026, 10, 30), "approved", long_ago, NOW - 5 * hour, n=1
    )
    add_deadline(session, old, date(2026, 10, 31), "rejected", long_ago, NOW - 5 * hour, n=2)
    add_deadline(session, old, date(2026, 9, 1), created=long_ago, n=3)  # 候选日期已过
    approved.audit_state = "flagged"
    approved.audit_note = "正文里找不到"
    session.add(
        AICheck(
            document_id=apply_doc.id,
            task="kind",
            model="qwen-flash",
            result={"kind": "名单", "confidence": 0.8, "reason": "测试"},
            checked_at=NOW - hour,
        )
    )
    fresh = User(openid="o-fresh", created_at=NOW - 2 * hour, last_visited_at=NOW - hour)
    idle = User(
        openid="o-idle",
        created_at=NOW - timedelta(days=10),
        last_visited_at=NOW - timedelta(days=2),
    )
    session.add_all([fresh, idle])
    session.flush()
    session.add(Follow(user_id=fresh.id, name="科技", conditions={}, created_at=NOW - hour))
    for doc, status in ((apply_doc, "sent"), (list_doc, "failed")):
        session.add(
            Subscription(
                user_id=fresh.id,
                kind="deadline",
                template_id="t",
                document_id=doc.id,
                status=status,
                attempted_at=NOW - hour,
            )
        )
    session.add(
        Feedback(user_id=fresh.id, document_id=old.id, content="日期不对", check_result="ok")
    )
    session.add_all(
        [
            AIUsage(
                day=date(2026, 9, 28),
                task="check",
                model="qwen-flash",
                calls=12,
                errors=1,
                prompt_tokens=21000,
                completion_tokens=3200,
            ),
            AIUsage(
                day=date(2026, 9, 20),
                task="check",
                model="qwen-flash",
                calls=50,
                errors=0,
                prompt_tokens=100000,
                completion_tokens=5000,
            ),
        ]
    )
    session.commit()
    return {"apply": apply_doc, "list": list_doc, "old": old, "twin": twin}


def test_digest_numbers_match_the_consoles(session, world) -> None:
    d = build_digest(session, now=NOW)
    assert d.day == date(2026, 9, 29)
    assert (d.sources_total, d.sources_ok, len(d.source_failures)) == (2, 1, 1)
    # 基线、三天前的和同一通知的其他网址都不算新文件
    assert d.new_documents == 2 and d.new_by_kind == {"申报通知": 1, "名单": 1}
    assert (d.details_ok, d.details_failed, d.new_candidates) == (1, 1, 2)
    assert (d.approved, d.rejected) == (1, 1)
    assert (d.pending, d.pending_30, d.nearest_due) == (3, 1, date(2026, 10, 10))
    # 7 天内到期的待核对和管理台"截止日期 · 7 天内"同一个数；最近一次核对是今天（北京时间）
    assert d.pending_7 == 0 and d.last_reviewed == date(2026, 9, 29)
    assert (d.kinds_pending, d.audit_flagged, d.feedback_pending) == (1, 1, 1)
    assert (d.new_users, d.active_users, d.total_users, d.new_follows) == (1, 1, 2, 1)
    assert (d.reminders_sent, d.reminders_failed) == (1, 1)
    assert d.usage.day == date(2026, 9, 28) and d.usage.calls == 12

    text = digest_text(d, [])
    assert text.splitlines()[1] == (
        "今天先核：7 天内到期的截止待核对 0 条（管理台“截止日期 · 7 天内”）；上次核对 9月29日"
    )
    for fragment in (
        "运维日报 9月29日",
        "启用栏目 2 个，今天检查成功 1 个（50.0%）；异常 1 个：山东省科学技术厅2",
        # 异常栏目带上次成功的日期：从没成功过的写从哪天起
        "山东省科学技术厅2 · 通知公告 · http_error（从没成功过，9月29日起）",
        "24 小时新发现 2 份",
        "补详情成功 1、失败 1；新截止候选 2 条",
        "24 小时通过 1、驳回 1；待核对 3 条（30 天内截止 1 条，最近 10月10日）",
        "类型待定 1；质检存疑 1；纠错待处理 1",
        "用户：新增 1、24 小时活跃 1、累计 2；新关注 1；提醒发出 1、失败 1",
        "AI（9月28日）：调用 12 次、出错 1 次，输入 2.1 万、输出 3200 tokens，"
        "当日模型调用费最多 0.01 元（按 9月28日官网最高价估，实际可能更低）",
        "qwen-flash 累计 12.9 万 tokens",
    ):
        assert fragment in text, fragment
    # 群里只报总数，不出现任何用户标识
    assert "o-fresh" not in text and "AI 建议" not in text


def test_digest_prices_deepseek_without_a_free_quota(session, world) -> None:
    """DeepSeek 按官网最高价估费用上限（高峰时段、输入都按没命中缓存）；不写免费额度。"""
    session.add(
        AIUsage(
            day=date(2026, 9, 28),
            task="interpret",
            model="deepseek-flash",
            calls=3,
            errors=0,
            prompt_tokens=500_000,
            completion_tokens=100_000,
        )
    )
    session.commit()
    text = digest_text(build_digest(session, now=NOW), [])
    # deepseek-flash：0.5 × 2 + 0.1 × 8 = 1.80 元；加上 qwen-flash 那 12 次的约 0.008 元
    assert "，当日模型调用费最多 1.81 元（按 9月28日官网最高价估，实际可能更低）" in text
    assert "deepseek-flash 累计 60 万 tokens" in text
    # 免费额度不再写：百炼已经不用，DeepSeek 的赠送余额金额没公布
    assert "免费额度" not in text


def test_digest_without_usage_that_day_reports_only_cumulative(session, world) -> None:
    """前一天没有 AI 用量时只报累计，不写费用（也就没有核价日期）。"""
    text = digest_text(build_digest(session, now=NOW + timedelta(days=1)), [])
    line = next(row for row in text.splitlines() if row.startswith("AI（"))
    assert line.startswith("AI（9月29日）：调用 0 次、出错 0 次")
    assert "费用" not in line and "qwen-flash 累计 12.9 万 tokens" in line


def test_cost_cap_rounds_up_to_the_cent() -> None:
    """上限往上取到分，不会四舍五入成更小的数；浮点误差不多算一分。"""
    assert digest_module._cost_cap(0.002) == "0.01"
    assert digest_module._cost_cap(0.0049) == "0.01"
    assert digest_module._cost_cap(0.0) == "0.00"
    assert digest_module._cost_cap(1.8) == "1.80"
    assert digest_module._cost_cap(0.1 + 0.2) == "0.30"  # 0.30000000000000004
    assert digest_module._cost_cap(1.80795) == "1.81"


def test_first_line_counts_the_seven_day_queue_and_days_since_review(session, world) -> None:
    """日报第一行：今天要核几条（和管理台同一个计数），上次核对是几号、几天前。"""
    from youju.api import app as admin

    # 两天后截止的待核对候选：进 7 天队列
    add_deadline(session, world["apply"], date(2026, 10, 1), created=NOW - timedelta(hours=1), n=7)
    # 把核对时间改到 4 天前：日报要写"4 天前"
    for row in session.scalars(
        select(DocumentDeadline).where(DocumentDeadline.status != "pending")
    ):
        row.reviewed_at = NOW - timedelta(days=4)
    session.commit()
    d = build_digest(session, now=NOW)
    assert d.pending_7 == admin.pending_count(session, None, "7")["pending"] == 1
    # 日报按它自己的"今天"算：同一时刻换一天（7 天窗口外）就不算
    assert admin.pending_due_count(session, "7", date(2026, 10, 2)) == 0
    assert d.last_reviewed == date(2026, 9, 25)
    line = digest_text(d, []).splitlines()[1]
    assert line == (
        "今天先核：7 天内到期的截止待核对 1 条（管理台“截止日期 · 7 天内”）；上次核对 9月25日（4 天前）"
    )
    # 已通过的截止今天标为"已变更"（延期）：上次核对是今天，不是原来通过的那天
    changed = session.scalars(
        select(DocumentDeadline).where(DocumentDeadline.status == "approved")
    ).first()
    changed.status, changed.changed_at = "changed", NOW - timedelta(hours=1)
    changed.change_reason = "延期"
    changed.changed_to_document_id = world["apply"].id
    session.commit()
    assert build_digest(session, now=NOW).last_reviewed == date(2026, 9, 29)
    # 一条都没核对过
    for row in session.scalars(
        select(DocumentDeadline).where(DocumentDeadline.status != "pending")
    ):
        session.delete(row)
    session.commit()
    d = build_digest(session, now=NOW)
    assert d.last_reviewed is None
    assert digest_text(d, []).splitlines()[1].endswith("；还没核对过一条")


def test_longest_failing_sources_come_first(session, world) -> None:
    """日报只列前 3 个异常栏目：坏得最久的排前面，不会被新坏的挤掉。"""
    old_failure = add_source(session, 7, status="parse_empty")
    old_failure.last_success_at = NOW - timedelta(days=9)
    recent_failure = add_source(session, 8, status="network_error")
    # 第 2 天；夹具里的山东省科学技术厅2 从没成功过、今天第一次失败，是第 1 天
    recent_failure.last_success_at = NOW - timedelta(days=2)
    session.commit()
    failures = build_digest(session, now=NOW).source_failures
    assert failures[0] == "山东省科学技术厅7 · 通知公告 · parse_empty（上次成功 9月20日）"
    assert [f.split(" · ")[0] for f in failures] == [
        "山东省科学技术厅7",
        "山东省科学技术厅8",
        "山东省科学技术厅2",
    ]


def fake_ai(reply):
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if isinstance(reply, httpx.Response):
            return reply
        content = json.dumps({"picks": reply(payload)}, ensure_ascii=False)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 10},
            },
        )

    client = ChatClient(
        "https://example.test",
        "key",
        "qwen-flash",
        max_retries=0,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    return client, calls


def test_ai_picks_only_from_the_offered_list(session, world) -> None:
    apply_doc, list_doc, old = world["apply"], world["list"], world["old"]
    client, calls = fake_ai(
        lambda _: [
            {"id": 999999, "reason": "编的编号"},
            {"id": list_doc.id, "reason": "面向全省企业的资金项目，覆盖面广而且截止时间比较近"},
            {"id": list_doc.id, "reason": "重复"},
            {"id": apply_doc.id},
            {"id": old.id, "reason": "候选日期已过，不在清单里"},
        ]
    )
    picks = pick_for_review(session, client, now=NOW)
    assert [p.document_id for p in picks] == [list_doc.id, apply_doc.id]
    assert picks[0].reason == "面向全省企业的资金项目，覆盖面广而且截止时间比较近"[:20]
    assert picks[1].reason == "" and picks[1].due == date(2026, 10, 10)
    sent = calls[0]["messages"][1]["content"]
    # 只发政府通知的编号、单位、标题和候选日期，候选日期已过的不发，也没有任何用户数据
    assert f"编号 {apply_doc.id}" in sent and "候选截止 2026-10-10" in sent
    assert f"编号 {old.id}" not in sent and "o-fresh" not in sent and "日期不对" not in sent
    assert picks[0].dept == "山东省科学技术厅1"

    text = digest_text(build_digest(session, now=NOW), picks)
    assert "AI 建议先核对（仅供参考）：" in text
    assert f"①《{LIST[:22]}》（山东省科学技术厅1）候选 12月20日截止：" in text


def test_ai_errors_leave_the_digest_without_picks(session, world) -> None:
    client, _ = fake_ai(httpx.Response(500, json={}))
    assert pick_for_review(session, client, now=NOW) == []


def test_digest_drops_ai_picks_first_when_the_message_is_too_long(
    session, world, monkeypatch
) -> None:
    d = build_digest(session, now=NOW)
    picks = [
        digest_module.Pick(
            world["apply"].id, APPLY * 2, "山东省科学技术厅1", date(2026, 10, 10), "截止最近" * 5
        )
        for _ in range(3)
    ]
    base = digest_text(d, [])
    prefix = len("【有据】".encode())
    monkeypatch.setattr(digest_module, "MAX_ALERT_BYTES", len(base.encode()) + prefix + 260)
    text = digest_text(d, picks)
    assert text.startswith(base) and text.count("《") == 1
    assert len(text.encode()) <= len(base.encode()) + 260
    monkeypatch.setattr(digest_module, "MAX_ALERT_BYTES", len(base.encode()) + prefix + 10)
    assert digest_text(d, picks) == base  # 一条都放不下时连标题也不加
    # 数字部分本身超长时截断，保证不超过群消息上限
    monkeypatch.setattr(digest_module, "MAX_ALERT_BYTES", len(base.encode()) // 2)
    cut = digest_text(d, picks)
    assert cut.endswith("…") and len(cut.encode()) <= len(base.encode()) // 2 - prefix


def _freeze_cli_clock(monkeypatch, cli) -> None:
    """日报命令用真实的当前时间；测试数据里的候选截止日期是写死的（10 月 10 日、12 月 20 日），
    过了那天"候选截止还没过"的通知就没了。把命令看到的时间固定在 NOW。"""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(cli, "datetime", Frozen)


def test_daily_report_cli_sends_once_and_records_ai_usage(
    session, session_factory, world, monkeypatch, capsys
) -> None:
    import youju.ai as ai_module
    from youju import alerts, cli

    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    _freeze_cli_clock(monkeypatch, cli)
    monkeypatch.setattr(
        ai_module, "get_chat_client", lambda: fake_ai(lambda _: [{"id": world["apply"].id}])[0]
    )
    sent = []
    monkeypatch.setattr(alerts, "send_alert", lambda message: sent.append(message) or True)
    assert cli.main(["daily-report", "--dry-run"]) == 0 and sent == []
    assert "运维日报" in capsys.readouterr().out
    assert cli.main(["daily-report"]) == 0 and len(sent) == 1
    assert "AI 建议先核对" in sent[0]
    usage = session.scalars(select(AIUsage).where(AIUsage.task == "report")).all()
    assert sum(u.calls for u in usage) == 2 and sum(u.prompt_tokens for u in usage) == 100

    monkeypatch.setattr(ai_module, "get_chat_client", lambda: None)  # 没配 AI 时日报照发
    monkeypatch.setattr(alerts, "send_alert", lambda message: False)
    assert cli.main(["daily-report"]) == 1  # 发送失败按任务失败退出，由 job.sh 再提醒


def test_usage_that_cannot_be_recorded_fails_the_command(
    session_factory, world, monkeypatch, capsys
) -> None:
    import youju.ai as ai_module
    import youju.ai.usage as usage_module
    from youju import cli

    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    _freeze_cli_clock(monkeypatch, cli)
    monkeypatch.setattr(
        ai_module, "get_chat_client", lambda: fake_ai(lambda _: [{"id": world["apply"].id}])[0]
    )
    tries = []

    def broken(*args, **kwargs):
        tries.append(1)
        raise RuntimeError("数据库断开")

    monkeypatch.setattr(usage_module, "record_usage", broken)
    assert cli.main(["daily-report", "--dry-run"]) == 1 and len(tries) == 2  # 重试一次
    out = capsys.readouterr().out
    assert "AI 用量没记进台账（report）：调用 1 次" in out and "输入 50" in out


def test_access_quality_and_usage_lines(session, world) -> None:
    """日报加三行：接口（昨天）、数据（近 30 天发布）、用量。"""
    before = build_digest(session, now=NOW)
    assert before.access == ()
    assert digest_text(before, []).count("接口（9月28日）：没有汇总") == 1
    # 昨天（北京时间 9/28）各服务的汇总；前天的不算
    session.add_all(
        [
            AccessDaily(day=date(2026, 9, 28), service="api", requests=1000, errors=1, slow=2),
            AccessDaily(day=date(2026, 9, 28), service="admin", requests=3000, errors=0, slow=1),
            AccessDaily(day=date(2026, 9, 27), service="api", requests=5, errors=5, slow=0),
        ]
    )
    # 申报通知有了足够长的正文（200 字）；名单解析出企业
    detail = session.scalars(
        select(DocumentDetail).where(DocumentDetail.document_id == world["apply"].id)
    ).one()
    detail.body_text = "正" * 200
    session.add(
        ListEntry(
            document_id=world["list"].id,
            source_url=world["list"].url,
            name="合成样例甲科技有限公司",
            normalized_name="合成样例甲科技有限公司",
            category="其他",
        )
    )
    # 附件副本：两个原站链接内容相同、共用一个对象（只算一次，3 MB）；没下载好的不算
    for n, status, size, file_id in (
        (1, "ready", 3 * 1024 * 1024, "cloud://env.bucket/attachments/a.pdf"),
        (2, "ready", 3 * 1024 * 1024, "cloud://env.bucket/attachments/a.pdf"),
        (3, "pending", None, None),
    ):
        session.add(
            AttachmentCopy(
                url_hash=f"{n}".ljust(64, "0"),
                url=f"https://kjt.shandong.gov.cn/att/{n}.pdf",
                declared_type="pdf",
                status=status,
                size=size,
                file_id=file_id,
            )
        )
    user = session.scalars(select(User).where(User.openid == "o-fresh")).one()
    for n, file_id in ((1, "cloud://env.bucket/advisor/1.xlsx"), (2, None)):
        session.add(
            AdvisorTask(
                user_id=user.id,
                kind="compare",
                status="done",
                row_count=1,
                idempotency_key=f"k{n}",
                result_file_id=file_id,
            )
        )
    session.commit()
    d = build_digest(session, now=NOW)
    assert d.access == (("admin", 3000, 0, 1), ("api", 1000, 1, 2))
    assert d.notices_with_body == before.notices_with_body + 1
    assert d.notices_30 == before.notices_30 and d.notices_with_candidates >= 1
    assert d.lists_parsed == before.lists_parsed + 1 and d.lists_30 >= 1
    assert d.attachment_copies == before.attachment_copies + 1
    assert d.attachment_bytes == before.attachment_bytes + 3 * 1024 * 1024
    assert d.result_files == before.result_files + 1
    assert d.db_bytes is not None and d.db_bytes > 0
    text = digest_text(d, [])
    # 向下取一位小数：2999/3000 写 99.9%，不会写成 100%
    assert (
        "接口（9月28日，小程序接口）：admin 3000 次，5xx 0 次、其他请求 3 秒及以上 1 次，3 秒内成功 99.9%；"
        "api 1000 次，5xx 1 次、其他请求 3 秒及以上 2 次，3 秒内成功 99.7%"
    ) in text
    assert "数据（近 30 天发布）：申报通知 " in text and "解析出企业名单 " in text
    assert "；附件副本 1 个，约 3 MB；批量工具结果文件 1 个" in text


def test_quality_line_wording() -> None:
    d = digest_module.Digest(
        day=date(2026, 10, 19),
        notices_30=207,
        notices_with_body=190,
        notices_with_candidates=150,
        lists_30=40,
        lists_parsed=28,
        db_bytes=None,
    )
    text = digest_text(d, [])
    # 百分比向下取整；数据库大小取不到时照写
    assert (
        "数据（近 30 天发布）：申报通知 207 份，有正文 91%、有截止候选 72%；"
        "名单 40 份，解析出企业名单 70%"
    ) in text
    assert "用量：数据库大小取不到；附件副本 0 个，不到 1 MB；批量工具结果文件 0 个" in text
    empty = digest_text(digest_module.Digest(day=date(2026, 10, 19)), [])
    assert "申报通知 0 份，有正文 —、有截止候选 —；名单 0 份，解析出企业名单 —" in empty


def test_a_failing_metric_only_blanks_its_own_line(session, world, monkeypatch) -> None:
    """新加的统计哪一项出错（例如查一张没有的表），只把那一项写成取不到：这一项回滚到保存点，
    后面几项照常查，日报其他部分照发。"""
    from sqlalchemy import text

    def broken_quality(session, digest, today):
        session.execute(text("SELECT * FROM no_such_table_for_test"))

    monkeypatch.setattr(digest_module, "_quality", broken_quality)
    session.add(AccessDaily(day=date(2026, 9, 28), service="api", requests=10, errors=0, slow=0))
    session.commit()
    d = build_digest(session, now=NOW)
    assert (d.access_ok, d.quality_ok, d.usage_ok) == (True, False, True)
    assert d.access == (("api", 10, 0, 0),) and d.db_bytes
    text_ = digest_text(d, [])
    assert "数据（近 30 天发布）：取不到（统计出错，看容器日志）" in text_
    assert "接口（9月28日，小程序接口）：api 10 次" in text_ and "用量：数据库约" in text_
    # 前面算好的数不受影响
    assert "今天先核：" in text_ and "待核对 3 条" in text_


def test_quality_counts_content_on_other_urls_of_the_same_notice(session, world) -> None:
    """正文、截止候选在同一通知的另一个网址上（归并组），小程序会用，日报也要算上。"""
    apply_doc, twin = world["apply"], world["twin"]
    before = build_digest(session, now=NOW)
    # 主文件自己没有正文（只有两个字，按详情页规则用自己的），先换成空的；另一个网址上有 300 字
    detail = session.scalars(
        select(DocumentDetail).where(DocumentDetail.document_id == apply_doc.id)
    ).one()
    detail.body_text = ""
    session.add(
        DocumentDetail(document_id=twin.id, body_text="正" * 300, fetched_at=NOW, status="ok")
    )
    session.commit()
    after = build_digest(session, now=NOW)
    assert after.notices_with_body == before.notices_with_body + 1
    # 主文件自己有正文（哪怕短）就用自己的：和详情页一致，不够 200 字不算
    detail.body_text = "正" * 50
    session.commit()
    assert build_digest(session, now=NOW).notices_with_body == before.notices_with_body
    # 和详情页同一个清理：原文 205 字，去掉网页上的"【关闭窗口】"后只剩 199 字，不算
    detail.body_text = "正" * 199 + "【关闭窗口】"
    session.commit()
    assert build_digest(session, now=NOW).notices_with_body == before.notices_with_body
    detail.body_text = "正" * 200 + "【关闭窗口】"
    session.commit()
    assert build_digest(session, now=NOW).notices_with_body == before.notices_with_body + 1
