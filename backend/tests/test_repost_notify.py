"""关注、订阅与周报复用对外转发组规则，地区匹配及截止保持逐版本。"""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests import test_me as users
from tests import test_weekly as reports
from tests.test_me import client as client
from youju import digest, weekly
from youju.api import me, v1
from youju.models import (
    DigestProfile,
    Follow,
    FollowRepostNotification,
    RepostGroup,
    RepostGroupMember,
    Source,
    Subscription,
)
from youju.notify import _new_docs, run_notify


def group(session, docs, *, confirmed=True, review=False):
    row = RepostGroup(topic="项目申报", strength="strong", needs_review=review)
    session.add(row)
    session.flush()
    for doc in docs:
        session.add(
            RepostGroupMember(
                group_id=row.id,
                document_id=doc.id,
                confirmed_at=users.NOW if confirmed else None,
                confirmed_by="核对人" if confirmed else None,
            )
        )
    session.commit()
    return row


def followed_world(client, session, monkeypatch, conditions=None):
    users.settings(monkeypatch)
    users.agree(client)
    follow_id = users.follow(client, conditions).json()["id"]
    docs = []
    for n, province in enumerate(("江西", "山东", "国家")):
        doc = users.source_and_doc(
            session,
            title=f"{province}关于项目申报的通知",
            first_seen=users.NOW + timedelta(minutes=n + 1),
            due=users.NOW.date() + timedelta(days=n + 1),
        )
        src = session.get(Source, doc.source_id)
        src.province = province
        doc.publish_date = users.NOW.date() + timedelta(days=n)
        docs.append(doc)
    session.commit()
    return follow_id, docs


@pytest.mark.parametrize(
    "confirmed,review,total", [(False, False, 3), (True, True, 3), (True, False, 1)]
)
def test_follow_count_list_paging_home(client, session, monkeypatch, confirmed, review, total):
    follow_id, docs = followed_world(client, session, monkeypatch)
    group(session, docs, confirmed=confirmed, review=review)
    assert client.get("/api/v1/me/follows").json()["items"][0]["newCount"] == total
    values, _ = _new_docs(session, SimpleNamespace(follow_id=follow_id))
    assert values["count"] == total
    url = f"/api/v1/me/follows/{follow_id}/new"
    response = client.get(url, params={"limit": 1}).json()
    assert response["total"] == total and len(response["items"]) == 1
    assert client.get(url, params={"offset": total}).json()["items"] == []
    if total == 1:
        item = response["items"][0]
        assert item["id"] == str(docs[2].id)  # 部里原件优先于最早地方版
        assert item["group"]["versions"] == 2
        assert item["deadline"]["date"] == "2026-10-01"
    else:
        assert all("group" not in item for item in response["items"])
    # 重叠关注也只计一次，本周计数不受已读影响。
    users.follow(client, {"regions": "山东", "time": "不限"})
    assert client.get("/api/v1/me/follows").json()["newTotal"] == total
    assert client.get("/api/v1/home", params={"group": 1}).json()["stats"]["followed_new"] == total
    client.post(f"/api/v1/me/follows/{follow_id}/seen")
    assert client.get("/api/v1/home").json()["stats"]["followed_new"] == total


@pytest.mark.parametrize(
    "conditions,selected",
    [
        ({"regions": "山东", "time": "不限"}, 1),
        ({"q": "山东", "time": "不限"}, 1),
        ({"time": "不限"}, 2),
    ],
)
def test_notify_matches_before_grouping_and_consumes_once(
    client, session, monkeypatch, conditions, selected
):
    follow_id, docs = followed_world(client, session, monkeypatch, conditions)
    group(session, docs)
    item = client.get(f"/api/v1/me/follows/{follow_id}/new").json()["items"][0]
    assert item["id"] == str(docs[selected].id) and item["group"]["versions"] == 2
    assert item["deadline"]["date"] == (users.NOW.date() + timedelta(days=selected + 1)).isoformat()
    config = SimpleNamespace(
        send_wx_messages=True,
        template_newdocs={"id": "n", "fields": {"thing1": "summary", "number2": "count"}},
        template_deadline=None,
        agreement_version="2026-09",
    )
    user_id = session.get(Follow, follow_id).user_id
    for _ in range(2):
        session.add(
            Subscription(user_id=user_id, follow_id=follow_id, kind="newDocs", template_id="n")
        )
    session.commit()
    payloads = []
    stamp = users.NOW + timedelta(hours=1)
    result = run_notify(
        session, settings=config, sender=lambda p: payloads.append(p) or {"errcode": 0}, stamp=stamp
    )
    assert result["sent"] == 1 and len(payloads) == 1
    assert len(session.scalars(select(FollowRepostNotification)).all()) == 1
    assert payloads[0]["data"]["number2"]["value"] == "1"
    assert "另有 2 个转发版本" in payloads[0]["data"]["thing1"]["value"]
    assert payloads[0]["page"] == f"pages/detail/index?id={docs[selected].id}"
    assert (
        run_notify(session, settings=config, sender=lambda _: pytest.fail("重复发送"), stamp=stamp)[
            "sent"
        ]
        == 0
    )
    # 同组随后新增的版本不再重复提醒。
    new_doc = users.source_and_doc(session, first_seen=stamp + timedelta(minutes=1))
    src = session.get(Source, new_doc.source_id)
    src.province = "山东"
    new_doc.title = "山东新的转发项目申报通知"
    membership = session.scalar(
        select(RepostGroupMember).where(RepostGroupMember.document_id == docs[0].id)
    )
    session.add(
        RepostGroupMember(
            document_id=new_doc.id,
            group_id=membership.group_id,
            confirmed_at=stamp,
            confirmed_by="核对人",
        )
    )
    session.commit()
    assert (
        v1._count(session, me.notification_statement(session, session.get(Follow, follow_id))) == 0
    )


@pytest.mark.parametrize("conditions", [{"regions": "山东", "time": "不限"}, {"time": "不限"}])
def test_historical_versions_without_sent_record_do_not_suppress_new_versions(
    client, session, monkeypatch, conditions
):
    follow_id, docs = followed_world(client, session, monkeypatch, conditions)
    group(session, docs)
    follow = session.get(Follow, follow_id)
    follow.last_notified_at = users.NOW + timedelta(minutes=1, seconds=30)
    session.commit()
    assert v1._count(session, me.notification_statement(session, follow)) == 1


@pytest.mark.parametrize(
    "confirmed,review,count", [(False, False, 2), (True, True, 2), (True, False, 1)]
)
def test_weekly_folds_publications_keeps_version_deadlines(session, confirmed, review, count):
    docs = [
        reports.document(
            session, reports.source(session, province), title=f"{province}项目申报通知"
        )
        for province in ("江西", "山东")
    ]
    dues = [reports.deadline(session, doc, due=date(2026, 10, 8 + n)) for n, doc in enumerate(docs)]
    group(session, docs, confirmed=confirmed, review=review)
    profile = DigestProfile(name="全国版", columns={"applications": True, "deadlines": True})
    session.add(profile)
    session.commit()
    result = weekly.generate(session, profile.id, reports.START, stamp=reports.NOW)
    assert result.issue.counts == {"applications": count, "deadlines": 2, "lists": 0, "policies": 0}
    for due in dues:
        assert due.approved_date.isoformat() in result.issue.body_text_with_links
    if count == 1:
        assert "另有 1 个转发版本" in result.issue.body_html
        assert "另有 1 个转发版本" in result.issue.body_text_with_links
    # 只命中山东关键词时仍列山东自己的标题、网址和截止。
    profile.keywords = ["山东"]
    profile.columns = {"applications": True}
    session.commit()
    local = weekly.generate(session, profile.id, reports.START, stamp=reports.NOW).issue
    assert local.document_ids == [docs[1].id]
    assert (
        docs[1].url in local.body_text_with_links and docs[0].url not in local.body_text_with_links
    )
    assert (
        "2026-10-09" in local.body_text_with_links
        and "2026-10-08" not in local.body_text_with_links
    )
    # 运维日报仅新文件份数按组算，截止核对待办口径保持原样。
    report = digest.build_digest(session, now=reports.NOW + timedelta(hours=1))
    assert report.new_documents == count


def test_follow_excludes_removed_members_and_folds_guides_by_group(client, session, monkeypatch):
    follow_id, docs = followed_world(client, session, monkeypatch)
    group(session, docs)
    for doc in docs:
        doc.kind_override = "资讯与指南"
    session.commit()
    url = f"/api/v1/me/follows/{follow_id}/new"
    folded = client.get(url, params={"fold": 1}).json()
    assert (folded["total"], folded["folded"], folded["guides"]) == (0, 1, 1)
    shown = client.get(url, params={"fold": 0}).json()
    assert (shown["total"], shown["folded"], shown["guides"]) == (1, 0, 1)
    member = session.get(RepostGroupMember, docs[2].id)
    member.active = False
    session.commit()
    shown = client.get(url, params={"fold": 0}).json()
    assert shown["total"] == 2 and shown["guides"] == 2
    grouped = next(item for item in shown["items"] if "group" in item)
    assert grouped["group"]["versions"] == 1
    assert any(item["id"] == str(docs[2].id) and "group" not in item for item in shown["items"])


def test_group_with_only_new_matching_local_member_is_counted(client, session, monkeypatch):
    follow_id, docs = followed_world(
        client, session, monkeypatch, {"regions": "山东", "time": "不限"}
    )
    group(session, docs)
    # 江西和部里都早于关注建立；新发现的山东版仍须出现，不能先选全组主条目再筛新增。
    docs[0].first_seen_at = docs[2].first_seen_at = users.NOW - timedelta(days=1)
    session.commit()
    response = client.get(f"/api/v1/me/follows/{follow_id}/new").json()
    assert response["total"] == 1 and response["items"][0]["id"] == str(docs[1].id)
    assert response["items"][0]["deadline"]["date"] == "2026-09-30"


@pytest.mark.parametrize("field", ["name", "title", "summary"])
def test_group_notice_survives_template_length_limit(monkeypatch, field):
    from youju import notify

    follow = SimpleNamespace(id=1, name="只关注山东的项目申报条件" * 3)
    doc = SimpleNamespace(id=12, title="山东关于组织项目申报的通知" * 3)
    subscription = SimpleNamespace(follow_id=1, template_id="new-docs")
    session = SimpleNamespace(
        get=lambda *_: follow,
        execute=lambda _: SimpleNamespace(one=lambda: (doc, None)),
        scalars=lambda _: SimpleNamespace(all=lambda: [7]),
    )
    monkeypatch.setattr(notify, "notification_statement", lambda *_: select(Source))
    monkeypatch.setattr(v1, "_count", lambda *_: 1)
    monkeypatch.setattr(v1, "_ordered", lambda stmt: stmt)
    monkeypatch.setattr(v1, "repost_versions", lambda *_: {12: {"others": [{}, {}]}})
    values, page = notify._new_docs(session, subscription)
    payload = notify._payload(
        subscription,
        SimpleNamespace(openid="unit-test-user"),
        {"fields": {"thing1": field, "number2": "count"}},
        values,
        page,
    )
    assert payload["page"] == "pages/detail/index?id=12"
    assert payload["data"]["number2"]["value"] == "1"
    text = payload["data"]["thing1"]["value"]
    assert len(text) <= 20 and "另有 2 个转发版本" in text


def test_weekly_group_annotation_in_all_rendered_formats():
    own_due = SimpleNamespace(
        kind="企业申报截止", approved_date=date(2026, 10, 9), approved_time=None
    )
    item = weekly.Item(
        12,
        "山东项目申报通知",
        "山东科技部门",
        "山东",
        reports.START,
        "",
        "https://example.gov.cn/12",
        deadlines=(own_due,),
        versions=2,
    )
    profile = SimpleNamespace(name="山东版", columns={"applications": True})
    columns = {key: [item] if key == "applications" else [] for key in weekly.COLUMNS}
    _, html, text, linked, counts, ids = weekly.render(
        profile, reports.START, reports.START + timedelta(days=6), reports.NOW.date(), columns
    )
    assert counts["applications"] == 1 and ids == [12]
    for body in (html, text, linked):
        assert "另有 2 个转发版本" in body
        assert "10 月 8 日" not in body and "2026-10-08" not in body
    assert "2026-10-09" in linked and item.url in linked


def test_dry_run_failure_and_follow_deletion_do_not_leave_sent_groups(client, session, monkeypatch):
    follow_id, docs = followed_world(client, session, monkeypatch)
    group(session, docs)
    follow = session.get(Follow, follow_id)
    config = SimpleNamespace(
        send_wx_messages=False,
        template_newdocs={"id": "n", "fields": {"thing1": "summary"}},
        template_deadline=None,
        agreement_version="2026-09",
    )

    def authorize():
        session.add(
            Subscription(
                user_id=follow.user_id, follow_id=follow_id, kind="newDocs", template_id="n"
            )
        )
        session.commit()

    authorize()
    stamp = users.NOW + timedelta(hours=1)
    assert (
        run_notify(session, settings=config, sender=lambda _: pytest.fail("演练发送"), stamp=stamp)[
            "dry_run"
        ]
        == 1
    )
    assert session.scalars(select(FollowRepostNotification)).all() == []
    config.send_wx_messages = True
    assert (
        run_notify(session, settings=config, sender=lambda _: {"errcode": 500}, stamp=stamp)[
            "failed"
        ]
        == 1
    )
    assert session.scalars(select(FollowRepostNotification)).all() == []
    authorize()
    assert (
        run_notify(session, settings=config, sender=lambda _: {"errcode": 0}, stamp=stamp)["sent"]
        == 1
    )
    assert len(session.scalars(select(FollowRepostNotification)).all()) == 1
    session.delete(follow)
    session.commit()
    assert session.scalars(select(FollowRepostNotification)).all() == []
