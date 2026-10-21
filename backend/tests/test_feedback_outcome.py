"""纠错处理结果（差距清单 C1）：管理台标记处理时选结果、可写一句说明，提交的人在"我的数据"里看得到。

依据《网络信息内容生态治理规定》第十六条"及时受理处置公众投诉举报并反馈处理结果"。
"""

import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import OperationalError

from youju import rewards
from youju.api import app as admin
from youju.api import me
from youju.models import Feedback, User

from .test_user_journeys import ADMIN, _agree, _json
from .test_user_journeys import journey as journey

OLD = datetime(2026, 10, 1, 2, tzinfo=UTC)


def _submit(j, content):
    return _json(j.client.post("/api/v1/feedback", json={"content": content}), 201)["id"]


def _handle(j, feedback_id, status=200, **body):
    response = j.client.post(f"/api/admin/feedback/{feedback_id}/handle", headers=ADMIN, json=body)
    return _json(response, status)


def _unhandle(j, feedback_id):
    return _json(j.client.post(f"/api/admin/feedback/{feedback_id}/unhandle", headers=ADMIN))


def _mine(j):
    return {row["id"]: row for row in _json(j.client.get("/api/v1/me/data"))["feedback"]}


def _listed(j):
    rows = _json(j.client.get("/api/admin/feedback", headers=ADMIN, params={"status": "all"}))
    return {row["id"]: row for row in rows}


def _row(j, feedback_id):
    j.session.expire_all()
    return j.session.get(Feedback, feedback_id)


def test_outcome_choices_follow_the_feedback_kind(journey):
    j = journey
    _agree(j)
    correction = _submit(j, "截止日期写错了")
    coverage = _submit(j, "申请覆盖：增加某省工信厅通知公告栏目")
    payment = _submit(j, "支付问题：付了款没开通")
    listed = _listed(j)
    assert [c["value"] for c in listed[correction]["outcomeChoices"]] == [
        "fixed",
        "unchanged",
        "removed",
        "noted",
    ]
    assert [c["value"] for c in listed[coverage]["outcomeChoices"]] == [
        "added",
        "declined",
        "noted",
    ]
    assert [c["value"] for c in listed[payment]["outcomeChoices"]] == ["resolved"]
    assert listed[correction]["outcomeChoices"][1] == {
        "value": "unchanged",
        "label": "已核对，和原文一致，没有改",
    }
    assert all(row["outcome"] is None and row["reply"] is None for row in listed.values())
    # 不属于这类的结果、不存在的结果、没写结果：都不收，也不会因此标成已处理
    for feedback_id, outcome in (
        (correction, "added"),
        (coverage, "fixed"),
        (payment, "noted"),
        (correction, "bogus"),
    ):
        assert _handle(j, feedback_id, 422, outcome=outcome)["detail"] == "请按反馈类型选择处理结果"
    url = f"/api/admin/feedback/{correction}/handle"
    assert j.client.post(url, headers=ADMIN, json={}).status_code == 422
    assert j.client.post(url, headers=ADMIN).status_code == 422
    assert all(row["handledAt"] is None for row in _listed(j).values())
    # 支付问题只有"已处理"一种结果：不写说明用户不知道怎么解决的，不收
    assert (
        _handle(j, payment, 422, outcome="resolved", reply="  ")["detail"]
        == "选“已处理”时要写一句给用户的说明，例如怎么解决的"
    )
    for feedback_id, outcome, label in (
        (correction, "removed", "已下架这份文件"),
        (coverage, "declined", "暂不接入"),
        (payment, "resolved", "已处理"),
    ):
        handled = _handle(j, feedback_id, outcome=outcome, reply="合成说明")
        assert handled["outcomeLabel"] == label
        assert _mine(j)[feedback_id]["outcomeLabel"] == label


def test_reply_is_one_clean_line_shown_to_the_user_not_logged(journey, caplog):
    j = journey
    _agree(j)
    feedback_id = _submit(j, "截止日期写错了")
    before = _mine(j)[feedback_id]
    assert (before["handled"], before["outcome"], before["outcomeLabel"], before["reply"]) == (
        False,
        None,
        None,
        None,
    )
    assert _handle(j, feedback_id, 422, outcome="fixed", reply="长" * 201)
    assert _row(j, feedback_id).handled_at is None
    admin.audit_log.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger="youju.audit"):
            body = _handle(j, feedback_id, outcome="fixed", reply="  已改成\n11 月 30 日\t​ ")
    finally:
        admin.audit_log.removeHandler(caplog.handler)
    assert body == {
        "handled": True,
        "changed": True,
        "outcome": "fixed",
        "outcomeLabel": "已更正",
        "reply": "已改成 11 月 30 日",
    }
    # 审计只记结果代码和有没有写说明，不记说明原文
    assert '"outcome": "fixed"' in caplog.text and '"reply": true' in caplog.text
    assert "11 月 30 日" not in caplog.text
    mine = _mine(j)[feedback_id]
    assert (mine["handled"], mine["outcome"], mine["outcomeLabel"], mine["reply"]) == (
        True,
        "fixed",
        "已更正",
        "已改成 11 月 30 日",
    )
    listed = _listed(j)[feedback_id]
    assert (listed["outcome"], listed["outcomeLabel"], listed["reply"]) == (
        "fixed",
        "已更正",
        "已改成 11 月 30 日",
    )


def test_changing_and_withdrawing_an_outcome(journey):
    j = journey
    _agree(j)
    feedback_id = _submit(j, "截止日期写错了")
    _handle(j, feedback_id, outcome="unchanged", reply="原文就是这样写的")
    row = _row(j, feedback_id)
    row.handled_at = OLD
    j.session.commit()
    # 已处理的再提交一次 = 改结果或说明，处理时间不变；只写空白的说明不存
    assert _handle(j, feedback_id, outcome="fixed", reply="   ")["changed"] is True
    row = _row(j, feedback_id)
    assert (row.outcome, row.reply, row.handled_at) == ("fixed", None, OLD)
    assert _handle(j, feedback_id, outcome="fixed")["changed"] is False
    # 撤回：回到待处理，结果和说明一起清掉；再撤回一次什么也不变
    assert _unhandle(j, feedback_id) == {"handled": False, "changed": True}
    row = _row(j, feedback_id)
    assert (row.handled_at, row.outcome, row.reply) == (None, None, None)
    mine = _mine(j)[feedback_id]
    assert (mine["handled"], mine["outcome"], mine["reply"]) == (False, None, None)
    assert _unhandle(j, feedback_id) == {"handled": False, "changed": False}
    assert (
        _json(
            j.client.post(
                "/api/admin/feedback/99999/handle", headers=ADMIN, json={"outcome": "fixed"}
            ),
            404,
        )["detail"]
        == "没有找到反馈"
    )


def test_old_handled_feedback_without_outcome_still_reads(journey):
    """0038 之前标记处理的记录没有结果：管理台和"我的数据"照常显示已处理，结果为空。"""
    j = journey
    _agree(j)
    feedback_id = _submit(j, "截止日期写错了")
    row = _row(j, feedback_id)
    row.handled_at = OLD
    j.session.commit()
    mine = _mine(j)[feedback_id]
    assert (mine["handled"], mine["outcome"], mine["outcomeLabel"], mine["reply"]) == (
        True,
        None,
        None,
        None,
    )
    assert _listed(j)[feedback_id]["outcomeLabel"] is None
    # 补选结果：处理时间还是原来的
    assert _handle(j, feedback_id, outcome="noted")["changed"] is True
    assert _row(j, feedback_id).handled_at == OLD


def test_reward_records_the_outcome_unless_one_was_chosen(journey, monkeypatch):
    j = journey
    settings = j.settings.model_copy(
        update={
            "rewards_enabled": True,
            "correction_reward_enabled": True,
            "advisor_enabled": True,
        }
    )
    for module in (admin, me, rewards):
        if hasattr(module, "get_settings"):
            monkeypatch.setattr(module, "get_settings", lambda: settings)
    _agree(j)
    correction = _submit(j, "截止日期写错了")
    coverage = _submit(j, "申请覆盖：增加某省工信厅通知公告栏目")
    chosen = _submit(j, "这份文件不是申报通知")
    _handle(j, chosen, outcome="removed", reply="已下架")
    url = "/api/admin/feedback/{}/reward/{}"
    body = _json(j.client.post(url.format(correction, "correction"), headers=ADMIN))
    assert (body["outcome"], body["outcomeLabel"], body["reply"]) == ("fixed", "已更正", None)
    again = _json(j.client.post(url.format(correction, "correction"), headers=ADMIN))
    assert again["alreadyGranted"] is True and again["outcome"] == "fixed"
    source = _json(j.client.post(url.format(coverage, "source"), headers=ADMIN))
    assert source["outcomeLabel"] == "已接入这个栏目"
    kept = _json(j.client.post(url.format(chosen, "correction"), headers=ADMIN))
    assert (kept["outcome"], kept["reply"]) == ("removed", "已下架")
    mine = _mine(j)
    assert [mine[i]["outcomeLabel"] for i in (correction, coverage, chosen)] == [
        "已更正",
        "已接入这个栏目",
        "已下架这份文件",
    ]
    assert all(mine[i]["handled"] for i in (correction, coverage, chosen))
    # 送过天数的：结果只能改成采纳类的
    assert (
        _handle(j, correction, 422, outcome="unchanged")["detail"]
        == "这条已经送过天数，处理结果只能选“已更正”或“已下架这份文件”"
    )
    assert _handle(j, correction, outcome="removed")["changed"] is True
    assert _handle(j, coverage, 422, outcome="declined")["detail"] == (
        "这条已经送过天数，处理结果只能选“已接入这个栏目”"
    )


def test_reward_is_refused_when_the_chosen_outcome_says_otherwise(journey, monkeypatch):
    """先选了"和原文一致，没有改""暂不接入"等，再点发奖：结果和送的天数对不上，不发。"""
    from sqlalchemy import func, select

    from youju.models import PassReward

    j = journey
    settings = j.settings.model_copy(
        update={
            "rewards_enabled": True,
            "correction_reward_enabled": True,
            "advisor_enabled": True,
        }
    )
    for module in (admin, me, rewards):
        if hasattr(module, "get_settings"):
            monkeypatch.setattr(module, "get_settings", lambda: settings)
    _agree(j)
    correction = _submit(j, "截止日期写错了")
    coverage = _submit(j, "申请覆盖：增加某省工信厅通知公告栏目")
    _handle(j, correction, outcome="unchanged", reply="原文就是这样写的")
    _handle(j, coverage, outcome="declined")
    url = "/api/admin/feedback/{}/reward/{}"
    refused = _json(j.client.post(url.format(correction, "correction"), headers=ADMIN), 422)
    assert refused["detail"] == (
        "已选的处理结果是“已核对，和原文一致，没有改”，和送天数对不上；"
        "确实要送，先把结果改成“已更正”或“已下架这份文件”"
    )
    assert _json(j.client.post(url.format(coverage, "source"), headers=ADMIN), 422)["detail"] == (
        "已选的处理结果是“暂不接入”，和送天数对不上；确实要送，先把结果改成“已接入这个栏目”"
    )
    j.session.expire_all()
    assert j.session.scalar(select(func.count(PassReward.id))) == 0
    assert _row(j, correction).outcome == "unchanged"
    # 改成采纳类的结果以后可以送
    _handle(j, correction, outcome="fixed", reply="已改正")
    assert _json(j.client.post(url.format(correction, "correction"), headers=ADMIN))["days"] == 1


def test_reward_saves_the_outcome_and_reply_from_the_form(journey, monkeypatch):
    """管理台发奖时带上表单里选的结果和写的说明：和发奖一起存；不是采纳类的结果不发。"""
    from sqlalchemy import func, select

    from youju.models import PassReward

    j = journey
    settings = j.settings.model_copy(
        update={
            "rewards_enabled": True,
            "correction_reward_enabled": True,
            "advisor_enabled": True,
        }
    )
    for module in (admin, me, rewards):
        if hasattr(module, "get_settings"):
            monkeypatch.setattr(module, "get_settings", lambda: settings)
    _agree(j)
    removed = _submit(j, "这份文件不是申报通知")
    plain = _submit(j, "截止日期写错了")
    url = "/api/admin/feedback/{}/reward/correction"
    refused = _json(
        j.client.post(
            url.format(removed), headers=ADMIN, json={"outcome": "unchanged", "reply": "合成"}
        ),
        422,
    )
    assert refused["detail"] == "送天数时处理结果只能选“已更正”或“已下架这份文件”"
    j.session.expire_all()
    assert j.session.scalar(select(func.count(PassReward.id))) == 0
    assert _row(j, removed).handled_at is None
    body = _json(
        j.client.post(
            url.format(removed),
            headers=ADMIN,
            json={"outcome": "removed", "reply": "这份是会议通知，\n已下架"},
        )
    )
    assert (body["outcome"], body["reply"]) == ("removed", "这份是会议通知， 已下架")
    # 没选结果：按"已更正"记，说明照存
    body = _json(
        j.client.post(url.format(plain), headers=ADMIN, json={"outcome": None, "reply": "已改正"})
    )
    assert (body["outcome"], body["outcomeLabel"], body["reply"]) == ("fixed", "已更正", "已改正")
    mine = _mine(j)
    assert (mine[removed]["outcomeLabel"], mine[removed]["reply"]) == (
        "已下架这份文件",
        "这份是会议通知， 已下架",
    )


def test_rewarded_feedback_cannot_be_withdrawn(journey, monkeypatch):
    """送过天数的不能撤回：天数不收回，撤回后用户看到"待处理"却拿了天数，对不上。"""
    j = journey
    settings = j.settings.model_copy(
        update={
            "rewards_enabled": True,
            "correction_reward_enabled": True,
            "advisor_enabled": True,
        }
    )
    for module in (admin, me, rewards):
        if hasattr(module, "get_settings"):
            monkeypatch.setattr(module, "get_settings", lambda: settings)
    _agree(j)
    feedback_id = _submit(j, "截止日期写错了")
    _json(j.client.post(f"/api/admin/feedback/{feedback_id}/reward/correction", headers=ADMIN))
    response = j.client.post(f"/api/admin/feedback/{feedback_id}/unhandle", headers=ADMIN)
    assert _json(response, 422)["detail"] == (
        "这条已经送过天数，不能撤回；要改处理结果，直接选采纳类的结果再保存"
    )
    row = _row(j, feedback_id)
    assert row.handled_at is not None and row.outcome == "fixed"
    # 没送过天数的照常能撤回
    other = _submit(j, "另一个日期错误")
    _handle(j, other, outcome="fixed")
    assert _unhandle(j, other) == {"handled": False, "changed": True}


def test_database_rejects_an_unknown_outcome(session):
    owner = User(openid="outcome-check")
    session.add(owner)
    session.commit()
    session.add(Feedback(user_id=owner.id, content="合成", check_result="pass", outcome="bogus"))
    with pytest.raises(OperationalError) as rejected:
        session.commit()
    assert rejected.value.orig.args[0] == 3819  # MySQL CHECK 约束违例
    session.rollback()


def test_console_page_has_outcome_controls(journey):
    page = journey.client.get("/admin/feedback").text
    assert "选择处理结果" in page and "给用户的说明" in page
    assert "我的 → 我的数据" in page
