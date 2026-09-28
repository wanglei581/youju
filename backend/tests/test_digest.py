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
    AICheck,
    AIUsage,
    Document,
    DocumentDeadline,
    DocumentDetail,
    DocumentMerge,
    Feedback,
    Follow,
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
    return {"apply": apply_doc, "list": list_doc, "old": old}


def test_digest_numbers_match_the_consoles(session, world) -> None:
    d = build_digest(session, now=NOW)
    assert d.day == date(2026, 9, 29)
    assert (d.sources_total, d.sources_ok, len(d.source_failures)) == (2, 1, 1)
    # 基线、三天前的和同一通知的其他网址都不算新文件
    assert d.new_documents == 2 and d.new_by_kind == {"申报通知": 1, "名单": 1}
    assert (d.details_ok, d.details_failed, d.new_candidates) == (1, 1, 2)
    assert (d.approved, d.rejected) == (1, 1)
    assert (d.pending, d.pending_30, d.nearest_due) == (3, 1, date(2026, 10, 10))
    assert (d.kinds_pending, d.audit_flagged, d.feedback_pending) == (1, 1, 1)
    assert (d.new_users, d.active_users, d.total_users, d.new_follows) == (1, 1, 2, 1)
    assert (d.reminders_sent, d.reminders_failed) == (1, 1)
    assert d.usage.day == date(2026, 9, 28) and d.usage.calls == 12

    text = digest_text(d, [])
    for fragment in (
        "运维日报 9月29日",
        "启用栏目 2 个，今天检查成功 1 个（50.0%）；异常 1 个：山东省科学技术厅2",
        "24 小时新发现 2 份",
        "补详情成功 1、失败 1；新截止候选 2 条",
        "24 小时通过 1、驳回 1；待核对 3 条（30 天内截止 1 条，最近 10月10日）",
        "类型待定 1；质检存疑 1；纠错待处理 1",
        "用户：新增 1、24 小时活跃 1、累计 2；新关注 1；提醒发出 1、失败 1",
        "AI（9月28日）：调用 12 次、出错 1 次，输入 2.1 万、输出 3200 tokens，约 0.01 元",
        "qwen-flash 累计 12.9 万 tokens（免费额度 100 万）",
    ):
        assert fragment in text, fragment
    # 群里只报总数，不出现任何用户标识
    assert "o-fresh" not in text and "AI 建议" not in text


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


def test_daily_report_cli_sends_once_and_records_ai_usage(
    session, session_factory, world, monkeypatch, capsys
) -> None:
    import youju.ai as ai_module
    from youju import alerts, cli

    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
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
