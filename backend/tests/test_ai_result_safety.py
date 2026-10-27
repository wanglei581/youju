"""AI 解读、条款对照的结果过两层内容安全（D-093 门槛第 4 条；带 AI 提审补齐清单第 4 道）。

结果存进去之前先过本地高危词库，再过微信内容安全接口；没通过的不显示、不存、退回次数，
另记一条处置记录。不向外网发请求：微信接口换成替身。
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from tests.test_ai_interpret import NOW, FakeChat, direct_task, item, run
from tests.test_ai_interpret import client as client  # noqa: F401
from tests.test_ai_interpret import doc as doc  # noqa: F401
from tests.test_ai_interpret import settings as settings  # noqa: F401
from youju import advisor_tasks as service
from youju import safety_words, wx_security
from youju.ai import result_safety
from youju.models import AdvisorUsage, AISafetyHit


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    """原地重试之间不真等。"""
    pauses = []
    monkeypatch.setattr(result_safety, "_pause", pauses.append)
    return pauses


@pytest.fixture
def wechat(monkeypatch):
    """微信内容安全的替身：默认通过，记下查了谁、查了什么。outcomes 里放好接下来几次的结果
    （Verdict 或 CheckDown），用完了照 verdict、down 回。"""
    state = SimpleNamespace(
        calls=[], verdict=wx_security.Verdict(True, "pass", None), down=False, outcomes=[]
    )

    def check_text(openid, content, **kwargs):
        state.calls.append((openid, content))
        if state.outcomes:
            outcome = state.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        if state.down:
            raise wx_security.CheckDown("测试：接口出错")
        return state.verdict

    monkeypatch.setattr(wx_security, "check_text", check_text)
    return state


def word_list(monkeypatch, word):
    """测试用的词库：只有一个词（取政府原文里本来就有的普通词，代替真的高危词）。"""
    library = safety_words.parse(
        {
            "version": "test-1",
            "final": True,
            "categories": {"test": {"name": "测试类别"}},
            "entries": [{"id": "t1", "category": "test", "word": word}],
        }
    )
    monkeypatch.setattr(safety_words, "LIBRARY", library)


def second_doc(session, doc):  # noqa: F811
    """再建一份通知：正文多一句，缓存键不同；原句照旧对得上。"""
    from tests.test_ai_interpret import BODY
    from youju.models import Document, DocumentDetail

    row = Document(
        source_id=doc.source_id,
        title="另一份申报通知",
        kind_override="申报通知",
        url="https://official.test/2",
        url_key="official.test/2",
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    session.add(row)
    session.flush()
    session.add(
        DocumentDetail(
            document_id=row.id, body_text=BODY + "\n另有说明。", fetched_at=NOW, status="ok"
        )
    )
    session.commit()
    return row


def used(session):
    session.expire_all()
    return session.scalar(select(func.coalesce(func.sum(AdvisorUsage.used_count), 0)))


def hits(session):
    session.expire_all()
    return list(session.scalars(select(AISafetyHit).order_by(AISafetyHit.id)))


def test_result_text_is_the_quotes_only():
    """查的是显示给用户的原句：条目的 text 和每条引用，去重；类型名和固定文字不查。"""
    result = {
        "items": [
            {"type": "材料", "text": "甲句。", "refs": [{"pid": "P1", "quote": "甲句。"}]},
            {"type": "对象", "text": "乙句。", "refs": [{"pid": "P2", "quote": "乙句。"}, "坏的"]},
            "坏的条目",
        ],
        "notice": "我们写的固定说明",
        "refusal": None,
    }
    assert result_safety.result_text(result, "items") == "甲句。\n乙句。"
    assert result_safety.result_text({"items": []}, "items") == ""
    assert result_safety.result_text(None, "items") == ""


def test_passed_result_is_stored(session, settings, doc, wechat):  # noqa: F811
    task = direct_task(session, doc)
    run(session, task, settings)
    assert task.status == "done" and task.result_json["items"]
    assert wechat.calls == [("internal", "请提交申请书及附件。")]
    assert used(session) == 1 and hits(session) == []
    # 通过的记上标记：词库版本、查没查微信（测试里不在云托管，不真调）
    assert task.result_json["safety"] == {"words": "draft-0", "wechat": False}


def test_local_word_blocks_result(session, settings, doc, wechat, monkeypatch):  # noqa: F811
    """本地词库命中：不显示、不存、退回次数、记处置记录，不再交给微信。"""
    word_list(monkeypatch, "申请书")
    task = direct_task(session, doc)
    run(session, task, settings)
    assert task.status == "failed" and task.error == result_safety.UNSAFE_MESSAGE
    assert task.result_json is None and task.result_rows == 0
    assert task.cache_key  # 留着缓存键：同一份内容 1 天内再提交直接按没通过处理
    assert wechat.calls == []
    assert used(session) == 0
    [hit] = hits(session)
    assert (hit.kind, hit.stage, hit.layer) == ("interpret", "answer", "local")
    reason = dict(hit.reason_json)
    assert reason.pop("bodyHash") == task.snapshot["body_hash"]  # 正文重抓以后也对得回
    assert reason == {"category": "test", "entry": "t1", "version": "test-1", "sid": "P003"}
    assert hit.text is None and hit.uid is None
    assert (hit.document_id, hit.task_id) == (doc.id, task.id)
    assert hit.expires_at == NOW + timedelta(days=190)


@pytest.mark.parametrize("suggest", ["risky", "review"])
def test_wechat_blocks_result(session, settings, doc, wechat, suggest):  # noqa: F811
    wechat.verdict = wx_security.Verdict(False, suggest, 20001)
    task = direct_task(session, doc)
    run(session, task, settings)
    assert task.status == "failed" and task.result_json is None
    assert used(session) == 0
    [hit] = hits(session)
    assert hit.layer == "wechat"
    assert {k: hit.reason_json[k] for k in ("suggest", "label")} == {
        "suggest": suggest,
        "label": 20001,
    }


def test_wechat_down_retries_in_place_then_fails(  # noqa: F811
    session, settings, doc, wechat, no_pause, monkeypatch
):
    """接口出错不放行：暂时性的原地重试（最多查 3 次），还不行就直接失败、退回次数，
    不重排整个任务（重排要重新调模型），不记处置记录，往群里提醒一次。"""
    sent = []
    monkeypatch.setattr("youju.alerts.send_alert", lambda message: sent.append(message) or True)
    monkeypatch.setattr(result_safety, "_alerted_at", [])
    wechat.down = True
    fake = FakeChat()
    task = direct_task(session, doc)
    run(session, task, settings, fake=fake)
    assert task.status == "failed" and task.error == result_safety.CHECK_DOWN_MESSAGE
    assert len(fake.calls) == 1 and len(wechat.calls) == 3 and len(no_pause) == 2
    assert used(session) == 0 and hits(session) == []
    assert len(sent) == 1 and "errcode None" in sent[0] and "申请书" not in sent[0]


def test_wechat_transient_error_then_passes(session, settings, doc, wechat):  # noqa: F811
    """系统繁忙（-1）这类暂时性错误：原地再查一次就过了，结果照常存。"""
    wechat.outcomes = [wx_security.CheckDown("测试：繁忙", -1)]
    fake = FakeChat()
    task = direct_task(session, doc)
    run(session, task, settings, fake=fake)
    assert task.status == "done" and len(fake.calls) == 1 and len(wechat.calls) == 2


def test_cached_result_is_checked_again(session, settings, doc, wechat, monkeypatch):  # noqa: F811
    """复用缓存的结果也照样查（开关打开以前缓存的从没查过）；没过的不复制给新任务，
    原来那份也撤下。"""
    first = direct_task(session, doc, key="first")
    run(session, first, settings)
    assert first.status == "done"
    fake = FakeChat()
    word_list(monkeypatch, "申请书")
    second = direct_task(session, doc, key="second")
    run(session, second, settings, fake=fake)
    assert fake.calls == []  # 复用了缓存，没再调模型
    assert second.status == "failed" and second.result_json is None
    session.refresh(first)
    assert first.status == "failed" and first.result_json is None  # 原来那份也撤下
    assert [h.task_id for h in hits(session)] == [second.id]


def test_blocked_cache_removes_shown_copies(session, settings, doc, wechat, monkeypatch):  # noqa: F811
    """复用的缓存后来没通过：撤下原来那份和它的复制件（别人看过的也撤），不再拿来复用；
    处置记录写明是从哪个任务的缓存复制来的。"""
    first = direct_task(session, doc, key="first")
    run(session, first, settings)
    copy_task = direct_task(session, doc, key="copy")
    run(session, copy_task, settings)
    assert copy_task.status == "done"
    word_list(monkeypatch, "申请书")
    third = direct_task(session, doc, key="third")
    run(session, third, settings)
    assert third.status == "failed" and third.error == result_safety.UNSAFE_MESSAGE
    for shown in (first, copy_task):
        session.refresh(shown)
        assert shown.status == "failed" and shown.error == result_safety.REMOVED_MESSAGE
        assert shown.result_json is None and shown.cache_key is None
    [hit] = hits(session)
    assert hit.task_id == third.id and hit.reason_json["cachedFrom"] in (first.id, copy_task.id)


@pytest.mark.parametrize("errcode", [45009, 61010, 40003, 43104, 47001, 40129])
def test_wechat_final_errors_do_not_retry(session, settings, doc, wechat, errcode):  # noqa: F811
    """重试也没用的错误（当天次数用完、用户两小时内没访问过、权限、参数等）：只查一次，直接失败。"""
    wechat.outcomes = [wx_security.CheckDown("测试：不可重试", errcode)]
    task = direct_task(session, doc)
    run(session, task, settings)
    assert task.status == "failed" and task.error == result_safety.CHECK_DOWN_MESSAGE
    assert len(wechat.calls) == 1 and used(session) == 0


def test_check_fails_closed(session, doc):  # noqa: F811
    """取不出要查的文字却有条目、提交的人已经注销：都不放行。"""
    from youju.models import AdvisorTask

    task = AdvisorTask(id=999, user_id=999999, kind="interpret", input_json={"doc_id": doc.id})
    with pytest.raises(ValueError):
        result_safety.check(session, task, {"items": [{"type": "材料"}]}, field="items", stamp=NOW)
    result = {"items": [{"type": "材料", "text": "一句原文。", "refs": []}]}
    with pytest.raises(LookupError):
        result_safety.check(session, task, result, field="items", stamp=NOW)


def test_refusal_is_not_checked(session, settings, doc, wechat):  # noqa: F811
    """模型给的原句一句都没对上（拒答）：只有我们写的固定说法，不用查。"""
    fake = FakeChat({"items": [item(text="原文里没有的一句话。")], "missing": [], "refusal": None})
    task = direct_task(session, doc)
    run(session, task, settings, fake=fake)
    assert task.status == "done" and task.result_json["refusal"]
    assert wechat.calls == [] and hits(session) == []


def test_api_shows_error_code(client, session, settings, doc, wechat, monkeypatch):  # noqa: F811
    word_list(monkeypatch, "申请书")
    task = direct_task(session, doc)
    run(session, task, settings)
    body = client.get(f"/api/v1/advisor/tasks/{task.id}").json()
    assert body["status"] == "failed" and body["result"] is None
    assert body["errorCode"] == "AI_UNSAFE" and body["error"] == result_safety.UNSAFE_MESSAGE


def test_cleanup_deletes_expired_records(session, settings, doc, wechat, monkeypatch):  # noqa: F811
    word_list(monkeypatch, "申请书")
    task = direct_task(session, doc)
    run(session, task, settings)
    session.add(
        AISafetyHit(
            created_at=NOW - timedelta(days=191),
            kind="clause",
            stage="answer",
            layer="wechat",
            expires_at=NOW - timedelta(days=1),
        )
    )
    session.commit()
    result = service.cleanup_advisor(session, settings=settings, stamp=NOW)
    assert result["ai_safety_hits"] == 1
    assert [h.task_id for h in hits(session)] == [task.id]
    # 删任务、注销都不动处置记录：190 天到期才删
    result = service.cleanup_advisor(session, settings=settings, stamp=NOW + timedelta(days=191))
    assert result["ai_safety_hits"] == 1 and hits(session) == []


def test_migration_0042_downgrade_upgrade(db_engine) -> None:
    from alembic import command
    from alembic.script import ScriptDirectory
    from sqlalchemy import inspect

    from youju.migrations import alembic_config

    from .conftest import TEST_DB_URL

    config = alembic_config(TEST_DB_URL)
    # 0042 接在哪个版本后面（合并时随 main 改 down_revision，测试不用跟着改）
    command.downgrade(
        config, ScriptDirectory.from_config(config).get_revision("0042").down_revision
    )
    assert "ai_safety_hits" not in inspect(db_engine).get_table_names()
    command.upgrade(config, "head")
    assert "ai_safety_hits" in inspect(db_engine).get_table_names()


def test_daily_report_counts_blocks(session, settings, doc, wechat, monkeypatch):  # noqa: F811
    """运维日报只报次数：拦下几次（按层）、检测接口出错几个；都是 0 不写这一行；不含任何文字。"""
    from youju.digest import build_digest, digest_text

    def safety_line(now):
        lines = digest_text(build_digest(session, now=now), []).splitlines()
        return [line for line in lines if line.startswith("内容安全")]

    assert safety_line(NOW) == []
    word_list(monkeypatch, "申请书")
    blocked = direct_task(session, doc, key="blocked")
    run(session, blocked, settings)
    monkeypatch.setattr(safety_words, "LIBRARY", safety_words.load())
    wechat.down = True
    monkeypatch.setattr("youju.alerts.send_alert", lambda message: True)
    other = second_doc(session, doc)  # 刚被拦过的那份 1 天内不再查，换一份
    down = direct_task(session, other, key="down")
    run(session, down, settings)
    assert down.error == result_safety.CHECK_DOWN_MESSAGE
    [line] = safety_line(NOW + timedelta(hours=1))
    assert "拦下 1 次（本地词库 1、微信 0）" in line
    assert "检测接口出错、AI 任务没做成 1 个" in line
    assert "申请书" not in line


def test_blocked_content_not_rechecked_within_a_day(  # noqa: F811
    session, settings, doc, wechat, monkeypatch
):
    """同一份内容没通过以后 1 天内再提交：直接按没通过处理，不再调模型和微信、不另写记录；
    过了 1 天照常再查。"""
    word_list(monkeypatch, "申请书")
    first = direct_task(session, doc, key="first")
    run(session, first, settings)
    assert first.status == "failed" and len(hits(session)) == 1
    fake = FakeChat()
    again = direct_task(session, doc, key="again")
    run(session, again, settings, fake=fake)
    assert again.status == "failed" and again.error == result_safety.UNSAFE_MESSAGE
    assert fake.calls == [] and wechat.calls == [] and len(hits(session)) == 1
    assert used(session) == 0
    later = direct_task(session, doc, key="later", stamp=NOW + timedelta(days=1, minutes=1))
    fake = FakeChat()
    run(session, later, settings, fake=fake, stamp=NOW + timedelta(days=1, minutes=1))
    assert len(fake.calls) == 1 and later.status == "failed" and len(hits(session)) == 2


def test_local_check_is_per_sentence():
    """本地词库一句一句查：前一句的结尾和后一句的开头拼不出词（交给微信的照旧是合起来的）。"""
    result = {
        "items": [
            {
                "type": "对象",
                "text": "由主管机关代",
                "refs": [{"pid": "P1", "quote": "由主管机关代"}],
            },
            {
                "type": "材料",
                "text": "开发票的说明",
                "refs": [{"pid": "P2", "quote": "开发票的说明"}],
            },
        ]
    }
    pairs = result_safety.quotes(result, "items")
    assert pairs == [("P1", "由主管机关代"), ("P2", "开发票的说明")]
    library = safety_words.parse(
        {
            "version": "t",
            "categories": {"test": {"name": "测试"}},
            "entries": [{"id": "t1", "category": "test", "word": "代开发票"}],
        }
    )
    assert result_safety.local_hit(pairs, library) is None
    assert safety_words.check(result_safety.result_text(result, "items"), library) is not None


def test_wechat_record_has_trace_id(session, settings, doc, wechat):  # noqa: F811
    wechat.verdict = wx_security.Verdict(False, "risky", 20001, "trace-abc")
    task = direct_task(session, doc)
    run(session, task, settings)
    [hit] = hits(session)
    assert hit.reason_json["traceId"] == "trace-abc"


def test_unchecked_results_hidden_on_read(client, session, settings, doc, wechat):  # noqa: F811
    """内容安全检查上线以前生成的结果（没有标记）：读的时候不显示，请重新提交。"""
    task = direct_task(session, doc)
    run(session, task, settings)
    task.result_json = {k: v for k, v in task.result_json.items() if k != "safety"}
    session.commit()
    body = client.get(f"/api/v1/advisor/tasks/{task.id}").json()
    assert body["result"] is None and body["error"] == result_safety.UNCHECKED_MESSAGE


def test_results_rechecked_after_word_list_update(  # noqa: F811
    client, session, settings, doc, wechat, monkeypatch
):
    """词库更新以后：读的时候先用新词库再查一遍，命中的不显示；每天的清理把它撤下、写处置记录，
    没命中的把标记改成新版本。"""
    hit_task = direct_task(session, doc, key="hit")
    run(session, hit_task, settings)
    assert hit_task.result_json["safety"]["words"] == "draft-0"
    word_list(monkeypatch, "申请书")  # 新版本 test-1 里有这个词
    body = client.get(f"/api/v1/advisor/tasks/{hit_task.id}").json()
    assert body["result"] is None and body["error"] == result_safety.REMOVED_MESSAGE
    result = service.cleanup_advisor(session, settings=settings, stamp=NOW)
    assert result["ai_results_removed"] == 1
    session.refresh(hit_task)
    assert hit_task.status == "failed" and hit_task.result_json is None
    [hit] = hits(session)
    assert hit.task_id == hit_task.id and hit.reason_json["sweep"] is True
    # 换一个查不到的词：没命中的把标记改成新版本，照常显示
    monkeypatch.setattr(safety_words, "LIBRARY", safety_words.load())
    clean = direct_task(session, doc, key="clean")
    run(session, clean, settings)
    word_list(monkeypatch, "不会出现的短语")
    assert client.get(f"/api/v1/advisor/tasks/{clean.id}").json()["result"]["items"]
    assert service.cleanup_advisor(session, settings=settings, stamp=NOW)["ai_results_removed"] == 0
    session.refresh(clean)
    assert clean.result_json["safety"]["words"] == "test-1"


def test_clause_result_keeps_mark():
    """条款对照复制、套企业自述时带着标记。"""
    from youju.ai import clause

    copied = clause.notification_result({"rows": [], "safety": {"words": "v", "wechat": True}})
    assert copied["safety"] == {"words": "v", "wechat": True}


def test_alert_throttled(monkeypatch):
    sent = []
    monkeypatch.setattr("youju.alerts.send_alert", lambda message: sent.append(message) or True)
    monkeypatch.setattr(result_safety, "_alerted_at", [])
    clock = [100.0]
    for _ in range(3):
        result_safety.alert_check_down(wx_security.CheckDown("x", 45009), clock=lambda: clock[0])
    assert len(sent) == 1 and "45009" in sent[0] and "100 次" in sent[0]
    clock[0] += 3601
    result_safety.alert_check_down(wx_security.CheckDown("x", -1), clock=lambda: clock[0])
    assert len(sent) == 2


def test_daily_report_scans_new_texts(session, settings, doc, monkeypatch):  # noqa: F811
    """日报把近 24 小时新抓的正文用本地词库扫一遍：命中的报份数（误拦，或网站被挂了违规内容）。"""
    from youju.digest import build_digest, digest_text

    word_list(monkeypatch, "申请书")
    lines = digest_text(build_digest(session, now=NOW + timedelta(hours=1)), []).splitlines()
    [line] = [line for line in lines if line.startswith("内容安全")]
    assert "新抓的正文 1 份、附件 0 份命中本地词库" in line and "申请书" not in line
    # 词库没有词条（随代码发布的草稿）时不扫、不写
    monkeypatch.setattr(safety_words, "LIBRARY", safety_words.load())
    lines = digest_text(build_digest(session, now=NOW + timedelta(hours=1)), []).splitlines()
    assert not [line for line in lines if line.startswith("内容安全")]


def test_safety_probe_counts_only(session, session_factory, settings, doc, monkeypatch, capsys):  # noqa: F811
    """开放前的只读量测：只打印各种结果的份数和没通过的通知编号，不打印正文。"""
    from youju import cli

    other = second_doc(session, doc)
    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    assert cli.main(["safety-probe", "--openid", "someone"]) == 1  # 本机不调微信
    assert "只能在云托管里跑" in capsys.readouterr().out
    monkeypatch.setattr(wx_security, "in_cloud", lambda settings: True)
    verdicts = iter(
        [wx_security.Verdict(False, "review", 20001), wx_security.Verdict(True, "pass", 100)]
    )
    seen = []

    def check_text(openid, content, **kwargs):
        seen.append(len(content))
        return next(verdicts)

    monkeypatch.setattr(wx_security, "check_text", check_text)
    assert cli.main(["safety-probe", "--openid", "someone", "--notices", "2"]) == 0
    out = capsys.readouterr().out
    assert "查了 2 份" in out and "review（label 20001） 1 份" in out and "出错 0 次" in out
    assert f"没通过的通知：{other.id}（review（label 20001））" in out
    assert "申请书" not in out and len(seen) == 2
