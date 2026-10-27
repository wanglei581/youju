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


@pytest.fixture
def wechat(monkeypatch):
    """微信内容安全的替身：默认通过，记下查了谁、查了什么。"""
    state = SimpleNamespace(calls=[], verdict=wx_security.Verdict(True, "pass", None), down=False)

    def check_text(openid, content, **kwargs):
        state.calls.append((openid, content))
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


def test_local_word_blocks_result(session, settings, doc, wechat, monkeypatch):  # noqa: F811
    """本地词库命中：不显示、不存、退回次数、记处置记录，不再交给微信。"""
    word_list(monkeypatch, "申请书")
    task = direct_task(session, doc)
    run(session, task, settings)
    assert task.status == "failed" and task.error == result_safety.UNSAFE_MESSAGE
    assert task.result_json is None and task.cache_key is None and task.result_rows == 0
    assert wechat.calls == []
    assert used(session) == 0
    [hit] = hits(session)
    assert (hit.kind, hit.stage, hit.layer) == ("interpret", "answer", "local")
    assert hit.reason_json == {"category": "test", "entry": "t1", "version": "test-1"}
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
    assert hit.layer == "wechat" and hit.reason_json == {"suggest": suggest, "label": 20001}


def test_wechat_down_retries_then_fails(session, settings, doc, wechat):  # noqa: F811
    """接口出错不放行：先重排（最多两次），重试用完记失败、退回次数，不记处置记录。"""
    wechat.down = True
    task = direct_task(session, doc)
    run(session, task, settings)
    assert task.status == "queued" and task.result_json is None
    assert used(session) == 1
    task.status, task.attempts, task.started_at = "running", 3, NOW
    session.commit()
    run(session, task, settings)
    assert task.status == "failed" and task.error == result_safety.CHECK_DOWN_MESSAGE
    assert used(session) == 0 and hits(session) == []


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


def test_wechat_final_errors_do_not_retry(session, settings, doc, monkeypatch):  # noqa: F811
    """用户两小时内没访问过（61010）、当天次数用完（45009）：重试也没用，直接失败、退回次数。"""

    def down(openid, content, **kwargs):
        raise wx_security.CheckDown("测试：次数用完", 45009)

    monkeypatch.setattr(wx_security, "check_text", down)
    task = direct_task(session, doc)
    run(session, task, settings)
    assert task.status == "failed" and task.error == result_safety.CHECK_DOWN_MESSAGE
    assert used(session) == 0


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
