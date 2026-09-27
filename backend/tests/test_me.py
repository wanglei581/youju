"""用户身份、个人接口、内容安全和提醒任务。"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from youju.api import me, v1
from youju.api.app import app
from youju.db import get_session
from youju.models import (
    Document,
    DocumentDeadline,
    DocumentMerge,
    Event,
    Feedback,
    Follow,
    Source,
    Subscription,
    User,
)
from youju.notify import run_notify

NOW = datetime(2026, 9, 28, 10, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def client(session_factory, monkeypatch):
    def override():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    monkeypatch.setattr(v1, "now", lambda: NOW)
    monkeypatch.setattr(
        me,
        "get_settings",
        lambda: SimpleNamespace(
            trust_wx_headers=False, dev_openid=None, template_newdocs=None, template_deadline=None
        ),
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def settings(monkeypatch, *, trust=False, openid="dev-user", deadline=None, newdocs=None):
    monkeypatch.setattr(
        me,
        "get_settings",
        lambda: SimpleNamespace(
            trust_wx_headers=trust,
            dev_openid=openid,
            template_deadline=deadline,
            template_newdocs=newdocs,
        ),
    )


def agree(client, headers=None):
    """同意用户协议和隐私保护指引（之后才能关注、订阅、纠错、埋点）。"""
    response = client.post("/api/v1/me/agreement", json={"version": "2026-09"}, headers=headers)
    assert response.status_code == 200


def source_and_doc(session, *, title="关于开展科技项目申报的通知", first_seen=None, due=None):
    source = Source(
        level="省级",
        province="北京",
        dept="科技",
        dept_full_name="北京市科技部门",
        column_name="通知",
        url=f"https://example.gov.cn/{session.query(Source).count()}/",
        parser_config={},
    )
    session.add(source)
    session.flush()
    doc = Document(
        source_id=source.id,
        url=f"{source.url}a.html",
        url_key=f"{source.id}/a.html",
        title=title,
        publish_date=NOW.date(),
        first_seen_at=first_seen or NOW + timedelta(minutes=1),
        last_seen_at=first_seen or NOW + timedelta(minutes=1),
    )
    session.add(doc)
    session.flush()
    if due is not None:
        session.add(
            DocumentDeadline(
                document_id=doc.id,
                fingerprint="due",
                kind="企业申报截止",
                candidate_date=due,
                approved_date=due,
                evidence="申报截止时间",
                confidence="高",
                status="approved",
                reviewed_by="审阅人",
                reviewed_at=NOW,
            )
        )
    session.commit()
    return doc


def follow(client, conditions=None):
    return client.post(
        "/api/v1/me/follows",
        json={"name": "科技项目", "conditions": conditions or {"time": "不限"}},
    )


def test_identity_modes_and_agreement(client, session, monkeypatch):
    assert client.get("/api/v1/me").status_code == 401
    assert client.post("/api/v1/feedback", json={"content": "纠错"}).status_code == 401
    assert client.post("/api/v1/events", json={"name": "search"}).status_code == 401
    settings(monkeypatch)
    assert client.get("/api/v1/me").json()["agreed"] is None
    # 同意之前：只查看不建用户记录；关注、纠错、埋点都不收
    assert session.scalar(select(func.count(User.id))) == 0
    assert client.post("/api/v1/feedback", json={"content": "纠错"}).status_code == 403
    assert client.post("/api/v1/events", json={"name": "search"}).status_code == 403
    assert client.get("/api/v1/me/follows").status_code == 403
    assert session.scalar(select(func.count(User.id))) == 0
    assert client.post("/api/v1/me/agreement", json={"version": "v" * 21}).status_code == 422
    agreed = client.post("/api/v1/me/agreement", json={"version": "2026-09"})
    assert agreed.status_code == 200
    assert client.get("/api/v1/me").json()["agreed"]["version"] == "2026-09"
    settings(monkeypatch, trust=True)
    assert client.get("/api/v1/me", headers={"X-WX-OPENID": "spoof"}).status_code == 401
    assert client.get("/api/v1/me", headers={"X-WX-SOURCE": "mini"}).status_code == 401
    headers = {"X-WX-SOURCE": "mini", "X-WX-FROM-OPENID": "wx-from"}
    assert client.get("/api/v1/me", headers=headers).status_code == 200
    assert (
        client.get("/api/v1/me", headers={**headers, "X-WX-OPENID": "wx-direct"}).status_code == 200
    )
    # 只有同意过的 dev-user 有记录
    assert session.scalar(select(func.count(User.id))) == 1


def test_follows_filters_new_seen_ownership_and_limit(client, session, monkeypatch):
    settings(monkeypatch)
    agree(client)
    bad = follow(client, {"regions": "火星"})
    assert bad.status_code == 422
    assert follow(client, {"time": "昨天"}).status_code == 422
    assert follow(client, {"q": "x" * 51}).status_code == 422
    created = follow(client)
    assert created.status_code == 201 and created.json()["newCount"] == 0
    follow_id = created.json()["id"]
    doc = source_and_doc(session)
    found = client.get(f"/api/v1/me/follows/{follow_id}/new").json()
    assert found["total"] == 1 and found["items"][0]["id"] == str(doc.id)
    assert client.get(f"/api/v1/me/follows/{follow_id}/new?limit=0").status_code == 422
    assert client.get(f"/api/v1/me/follows/{follow_id}/new?offset=1").json() == {
        "total": 1,
        "items": [],
    }
    assert client.get("/api/v1/me/follows").json()["items"][0]["newCount"] == 1
    # 同一文件的副本不增加计数。
    twin = Document(
        source_id=doc.source_id,
        url=f"{doc.source.url}b.html",
        url_key="twin",
        title=doc.title,
        publish_date=doc.publish_date,
        first_seen_at=doc.first_seen_at,
        last_seen_at=doc.first_seen_at,
    )
    session.add(twin)
    session.flush()
    session.add(
        DocumentMerge(duplicate_id=twin.id, canonical_id=doc.id, active=True, match_key="test")
    )
    session.commit()
    assert client.get(f"/api/v1/me/follows/{follow_id}/new").json()["total"] == 1
    monkeypatch.setattr(v1, "now", lambda: NOW + timedelta(minutes=2))
    assert client.post(f"/api/v1/me/follows/{follow_id}/seen").json() == {"newCount": 0}
    assert client.get(f"/api/v1/me/follows/{follow_id}/new").json()["total"] == 0
    settings(monkeypatch, openid="other")
    agree(client)
    assert client.get(f"/api/v1/me/follows/{follow_id}/new").status_code == 404
    assert client.post(f"/api/v1/me/follows/{follow_id}/seen").status_code == 404
    assert client.delete(f"/api/v1/me/follows/{follow_id}").status_code == 404
    settings(monkeypatch)
    agree(client)
    for i in range(19):
        assert follow(client, {"q": f"k{i}"}).status_code == 201
    assert follow(client).status_code == 422
    assert client.delete(f"/api/v1/me/follows/{follow_id}").status_code == 204


def test_subscriptions_deadline_change_and_delete(client, session, monkeypatch):
    settings(monkeypatch)
    agree(client)
    doc = source_and_doc(session, due=NOW.date() + timedelta(days=2))
    follow_id = follow(client).json()["id"]
    assert (
        client.post(
            "/api/v1/me/subscriptions", json={"kind": "newDocs", "templateId": "n"}
        ).status_code
        == 422
    )
    created = client.post(
        "/api/v1/me/subscriptions",
        json={"kind": "newDocs", "templateId": "n", "followId": follow_id},
    )
    assert created.status_code == 201
    assert (
        client.post(
            "/api/v1/me/subscriptions",
            json={"kind": "deadline", "templateId": "d", "docId": doc.id},
        ).status_code
        == 201
    )
    assert client.get("/api/v1/me").json()["subscriptions"] == {"newDocs": 1, "deadline": 1}
    assert len(client.get("/api/v1/me/subscriptions").json()["items"]) == 2
    assert client.delete(f"/api/v1/me/subscriptions/{created.json()['id']}").status_code == 204
    assert client.delete(f"/api/v1/me/subscriptions/{created.json()['id']}").status_code == 404
    # 同一归并组的副本已标变更，原件的 approved 不再有效。
    twin = Document(
        source_id=doc.source_id,
        url=f"{doc.source.url}b.html",
        url_key="changed-twin",
        title=doc.title,
        publish_date=doc.publish_date,
        first_seen_at=doc.first_seen_at,
        last_seen_at=doc.first_seen_at,
    )
    session.add(twin)
    session.flush()
    session.add(
        DocumentMerge(duplicate_id=twin.id, canonical_id=doc.id, active=True, match_key="test")
    )
    session.add(
        DocumentDeadline(
            document_id=twin.id,
            fingerprint="changed",
            kind="企业申报截止",
            candidate_date=NOW.date() + timedelta(days=2),
            approved_date=NOW.date() + timedelta(days=2),
            evidence="已延期",
            confidence="高",
            status="changed",
            change_reason="延期",
            changed_to_document_id=doc.id,
        )
    )
    session.commit()
    assert (
        client.post(
            "/api/v1/me/subscriptions",
            json={"kind": "deadline", "templateId": "d", "docId": doc.id},
        ).status_code
        == 422
    )
    settings(monkeypatch, openid="other")
    agree(client)
    assert (
        client.post(
            "/api/v1/me/subscriptions",
            json={"kind": "newDocs", "templateId": "n", "followId": follow_id},
        ).status_code
        == 404
    )


def test_feedback_security_and_events(client, session, monkeypatch):
    settings(monkeypatch)
    agree(client)
    assert client.post("/api/v1/feedback", json={"content": ""}).status_code == 422
    assert client.post("/api/v1/feedback", json={"content": "a" * 501}).status_code == 422
    assert (
        client.post("/api/v1/feedback", json={"docId": 999, "content": "有错"}).status_code == 422
    )
    assert client.post("/api/v1/feedback", json={"content": "请核对"}).status_code == 201
    assert session.scalar(select(Feedback.check_result)) == "unchecked"
    assert client.post("/api/v1/events", json={"name": "wrong"}).status_code == 422
    assert (
        client.post("/api/v1/events", json={"name": "search", "props": {"q": "敏感词"}}).status_code
        == 422
    )
    assert (
        client.post("/api/v1/events", json={"name": "search", "props": {"count": True}}).status_code
        == 422
    )
    for _ in range(60):
        assert client.post(
            "/api/v1/events", json={"name": "search", "props": {"count": 1}}
        ).json() == {"accepted": True}
    assert client.post("/api/v1/events", json={"name": "search"}).json() == {"accepted": False}
    assert session.scalar(select(func.count(Event.id))) == 60
    settings(monkeypatch, trust=True)
    headers = {"X-WX-SOURCE": "mini", "X-WX-OPENID": "wx"}
    agree(client, headers)

    class Response:
        def __init__(self, suggest="pass", errcode=0):
            self.suggest, self.errcode = suggest, errcode

        def raise_for_status(self):
            pass

        def json(self):
            return {"errcode": self.errcode, "result": {"suggest": self.suggest}}

    def passed(url, *, json, timeout):
        assert url == "http://api.weixin.qq.com/wxa/msg_sec_check"
        assert json == {"openid": "wx", "version": 2, "scene": 2, "content": "纠错"}
        return Response()

    monkeypatch.setattr(me.httpx, "post", passed)
    assert (
        client.post("/api/v1/feedback", json={"content": "纠错"}, headers=headers).status_code
        == 201
    )
    monkeypatch.setattr(me.httpx, "post", lambda *a, **kw: Response("review"))
    assert (
        client.post("/api/v1/feedback", json={"content": "纠错"}, headers=headers).status_code
        == 422
    )
    monkeypatch.setattr(me.httpx, "post", lambda *a, **kw: Response(errcode=500))
    assert (
        client.post("/api/v1/feedback", json={"content": "纠错"}, headers=headers).status_code
        == 503
    )
    monkeypatch.setattr(
        me.httpx, "post", lambda *a, **kw: (_ for _ in ()).throw(httpx.TimeoutException("timeout"))
    )
    assert (
        client.post("/api/v1/feedback", json={"content": "纠错"}, headers=headers).status_code
        == 503
    )
    assert session.scalar(select(func.count(Feedback.id))) == 2


def test_account_deletion_cascades(client, session, monkeypatch):
    settings(monkeypatch)
    agree(client)
    follow_id = follow(client).json()["id"]
    assert (
        client.post(
            "/api/v1/me/subscriptions",
            json={"kind": "newDocs", "templateId": "n", "followId": follow_id},
        ).status_code
        == 201
    )
    client.post("/api/v1/feedback", json={"content": "请核对"})
    client.post("/api/v1/events", json={"name": "feedback"})
    assert client.delete("/api/v1/me").status_code == 204
    for model in (User, Follow, Subscription, Feedback, Event):
        assert session.scalar(select(func.count()).select_from(model)) == 0


def test_notify_dry_run_and_one_shot_results(client, session, monkeypatch):
    settings(monkeypatch)
    agree(client)
    doc = source_and_doc(session, title="申报" * 15, due=NOW.date() + timedelta(days=2))
    follow_id = follow(client).json()["id"]
    session.get(Follow, follow_id).name = "科技项目" * 10
    session.commit()
    # 新文件在关注之后入库；演练和正式发送都能发现。
    n = client.post(
        "/api/v1/me/subscriptions",
        json={"kind": "newDocs", "templateId": "n", "followId": follow_id},
    ).json()["id"]
    d = client.post(
        "/api/v1/me/subscriptions", json={"kind": "deadline", "templateId": "d", "docId": doc.id}
    ).json()["id"]
    config = SimpleNamespace(
        send_wx_messages=False,
        template_newdocs={"id": "n", "fields": {"thing1": "summary", "number2": "count"}},
        template_deadline={"id": "d", "fields": {"thing1": "title", "date2": "date"}},
    )

    def forbidden(_):
        raise AssertionError("演练不得调用发送器")

    dry = run_notify(session, settings=config, sender=forbidden, stamp=NOW)
    assert dry["dry_run"] == 2
    assert [session.get(Subscription, i).status for i in (n, d)] == ["unused", "unused"]
    config.send_wx_messages = True
    payloads = []

    def sender(payload):
        payloads.append(payload)
        return {"errcode": 0 if len(payloads) == 1 else 43101}

    result = run_notify(session, settings=config, sender=sender, stamp=NOW)
    assert result["sent"] == 1 and result["refused"] == 1
    assert payloads[0]["page"] == "pages/follow/index"
    assert payloads[0]["data"]["thing1"]["value"].endswith("份新文件")
    assert len(payloads[1]["data"]["thing1"]["value"]) == 20
    assert payloads[1]["page"] == f"pages/detail/index?id={doc.id}"
    assert run_notify(session, settings=config, sender=forbidden, stamp=NOW)["sent"] == 0
    assert session.get(Follow, follow_id).last_notified_at == NOW
    assert session.get(Subscription, d).errcode == 43101


def test_notify_expired_failed_and_missing_template(client, session, monkeypatch):
    settings(monkeypatch)
    agree(client)
    doc = source_and_doc(session, due=NOW.date() + timedelta(days=1))
    sub_id = client.post(
        "/api/v1/me/subscriptions", json={"kind": "deadline", "templateId": "d", "docId": doc.id}
    ).json()["id"]
    config = SimpleNamespace(send_wx_messages=True, template_deadline=None, template_newdocs=None)
    assert (
        run_notify(session, settings=config, sender=lambda _: {"errcode": 0}, stamp=NOW)["skipped"]
        == 1
    )
    assert session.get(Subscription, sub_id).status == "unused"
    deadline = session.scalar(
        select(DocumentDeadline).where(DocumentDeadline.document_id == doc.id)
    )
    deadline.status = "changed"
    deadline.change_reason = "撤销"
    deadline.changed_to_document_id = doc.id
    session.commit()
    assert (
        run_notify(session, settings=config, sender=lambda _: {"errcode": 0}, stamp=NOW)["expired"]
        == 1
    )
    assert session.get(Subscription, sub_id).status == "expired"

    deadline.status = "approved"
    deadline.change_reason = None
    deadline.changed_to_document_id = None
    session.commit()
    second = client.post(
        "/api/v1/me/subscriptions", json={"kind": "deadline", "templateId": "d", "docId": doc.id}
    ).json()["id"]
    config.template_deadline = {"id": "d", "fields": {"thing1": "title"}}
    assert (
        run_notify(session, settings=config, sender=lambda _: {"errcode": 400}, stamp=NOW)["failed"]
        == 1
    )
    assert session.get(Subscription, second).errcode == 400


def test_deadline_today_with_past_time_expires(client, session, monkeypatch):
    settings(monkeypatch)
    agree(client)
    doc = source_and_doc(session, due=NOW.date())
    sub_id = client.post(
        "/api/v1/me/subscriptions", json={"kind": "deadline", "templateId": "d", "docId": doc.id}
    ).json()["id"]
    deadline = session.scalar(
        select(DocumentDeadline).where(DocumentDeadline.document_id == doc.id)
    )
    deadline.approved_time = datetime(2026, 9, 28, 9).time()
    session.commit()
    config = SimpleNamespace(send_wx_messages=True, template_deadline=None, template_newdocs=None)
    assert (
        run_notify(session, settings=config, sender=lambda _: {"errcode": 0}, stamp=NOW)["expired"]
        == 1
    )
    assert session.get(Subscription, sub_id).status == "expired"


def test_cleanup_removes_expired_personal_data(client, session, monkeypatch):
    """按保存期限删除：埋点 90 天、纠错 180 天、已用完的订阅授权 30 天；未用的授权和关注不动。"""
    from youju.notify import run_cleanup

    settings(monkeypatch)
    agree(client)
    user = session.scalar(select(User))
    old = NOW - timedelta(days=200)
    session.add_all(
        [
            Event(user_id=user.id, name="search", props={}, created_at=old),
            Event(user_id=user.id, name="search", props={}, created_at=NOW),
            Feedback(user_id=user.id, content="旧的", check_result="pass", created_at=old),
            Feedback(user_id=user.id, content="新的", check_result="pass", created_at=NOW),
        ]
    )
    follow_id = follow(client).json()["id"]
    used = Subscription(
        user_id=user.id,
        kind="newDocs",
        template_id="n",
        follow_id=follow_id,
        status="sent",
        attempted_at=old,
    )
    unused = Subscription(
        user_id=user.id, kind="newDocs", template_id="n", follow_id=follow_id, status="unused"
    )
    session.add_all([used, unused])
    session.commit()
    config = SimpleNamespace(
        retain_events_days=90, retain_feedback_days=180, retain_used_subscriptions_days=30
    )
    assert run_cleanup(session, settings=config, stamp=NOW) == {
        "events": 1,
        "feedback": 1,
        "subscriptions": 1,
    }
    assert session.scalar(select(func.count(Event.id))) == 1
    assert [f.content for f in session.scalars(select(Feedback))] == ["新的"]
    assert [s.status for s in session.scalars(select(Subscription))] == ["unused"]
    assert session.scalar(select(func.count(Follow.id))) == 1
