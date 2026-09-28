"""用户身份、个人接口、内容安全和提醒任务。"""

from datetime import datetime, time, timedelta
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
            trust_wx_headers=False,
            dev_openid=None,
            template_newdocs=None,
            template_deadline=None,
            agreement_version="2026-09",
            retain_events_days=90,
            retain_feedback_days=180,
            retain_used_subscriptions_days=30,
            retain_unused_subscriptions_days=365,
        ),
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def settings(
    monkeypatch, *, trust=False, openid="dev-user", deadline=None, newdocs=None, version="2026-09"
):
    monkeypatch.setattr(
        me,
        "get_settings",
        lambda: SimpleNamespace(
            trust_wx_headers=trust,
            dev_openid=openid,
            template_deadline=deadline,
            template_newdocs=newdocs,
            agreement_version=version,
            retain_events_days=90,
            retain_feedback_days=180,
            retain_used_subscriptions_days=30,
            retain_unused_subscriptions_days=365,
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


def test_mask_feedback_boundaries_without_database():
    assert me._mask_feedback("手机号 13812345678、138 1234 5678、138-1234-5678") == (
        "手机号 ［已隐去］、［已隐去］、［已隐去］"
    )
    assert me._mask_feedback("证件 11010519491231002X，邮箱 test.user+tag@example.com") == (
        "证件 ［已隐去］，邮箱 ［已隐去］"
    )
    assert me._mask_feedback("110105194912310021 11010519490230002X 913502001234567890") == (
        "110105194912310021 11010519490230002X 913502001234567890"
    )
    assert me._mask_feedback("0138123456780 A11010519491231002XB") == (
        "0138123456780 A11010519491231002XB"
    )
    # 邮箱前后是中文时只隐去邮箱本身
    assert me._mask_feedback("请发到abc@qq.com谢谢") == "请发到［已隐去］谢谢"
    # 带国家码的手机号
    assert me._mask_feedback("电话+8613812345678、+86 138 1234 5678、008613812345678") == (
        "电话［已隐去］、［已隐去］、［已隐去］"
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
    first = client.post(
        "/api/v1/me/subscriptions",
        json={"kind": "deadline", "templateId": "d", "docId": doc.id},
    )
    assert first.status_code == 201
    # 同一文件已有还没发出的截止提醒：返回已有的，不再新建
    again = client.post(
        "/api/v1/me/subscriptions",
        json={"kind": "deadline", "templateId": "d", "docId": doc.id},
    )
    assert again.status_code == 201
    assert again.json()["id"] == first.json()["id"]
    assert client.get("/api/v1/me").json()["subscriptions"] == {"newDocs": 1, "deadline": 1}
    titles = {
        s["kind"]: s["docTitle"] for s in client.get("/api/v1/me/data").json()["subscriptions"]
    }
    assert titles == {"newDocs": None, "deadline": doc.title}
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


def test_outdated_agreement_can_still_delete_own_data(client, session, monkeypatch):
    settings(monkeypatch)
    agree(client)
    doc = source_and_doc(session, due=NOW.date() + timedelta(days=5))
    follow_id = follow(client).json()["id"]
    sub_id = client.post(
        "/api/v1/me/subscriptions", json={"kind": "deadline", "templateId": "d", "docId": doc.id}
    ).json()["id"]
    # 协议升级，用户还没重新同意：不能新建，但能删除自己的关注和提醒授权
    settings(monkeypatch, version="2026-12")
    assert client.get("/api/v1/me/follows").status_code == 403
    assert client.delete(f"/api/v1/me/subscriptions/{sub_id}").status_code == 204
    assert client.delete(f"/api/v1/me/follows/{follow_id}").status_code == 204
    assert client.delete(f"/api/v1/me/follows/{follow_id}").status_code == 404
    # 从没同意过的人：404，不建记录
    settings(monkeypatch, openid="stranger")
    assert client.delete(f"/api/v1/me/follows/{follow_id}").status_code == 404
    assert session.scalar(select(func.count(User.id)).where(User.openid == "stranger")) == 0


def test_deadline_subscription_rejects_passed_time_today(client, session, monkeypatch):
    settings(monkeypatch)
    agree(client)
    doc = source_and_doc(session, due=NOW.date())
    deadline = session.scalar(
        select(DocumentDeadline).where(DocumentDeadline.document_id == doc.id)
    )
    body = {"kind": "deadline", "templateId": "d", "docId": doc.id}
    # 今天 9:00 截止，现在 10:00：已过，不接受
    deadline.approved_time = time(9, 0)
    session.commit()
    assert client.post("/api/v1/me/subscriptions", json=body).status_code == 422
    # 今天 17:00 截止：还没过，接受
    deadline.approved_time = time(17, 0)
    session.commit()
    assert client.post("/api/v1/me/subscriptions", json=body).status_code == 201


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


def test_personal_data_no_record_and_owned_rows(client, session, monkeypatch):
    settings(monkeypatch)
    empty = client.get("/api/v1/me/data")
    assert empty.status_code == 200
    assert empty.json() == {
        "user": None,
        "follows": [],
        "subscriptions": [],
        "subscriptionsTotal": 0,
        "feedback": [],
        "feedbackTotal": 0,
        "events": [],
        "eventsTotal": 0,
        "retention": {
            "events": 90,
            "feedback": 180,
            "usedSubscriptions": 30,
            "unusedSubscriptions": 365,
        },
        "exportedAt": NOW.isoformat(),
    }
    assert session.scalar(select(func.count(User.id))) == 0

    agree(client)
    owner = session.scalar(select(User).where(User.openid == "dev-user"))
    follow_id = follow(client).json()["id"]
    owned_follow = session.get(Follow, follow_id)
    owned_follow.last_seen_at = NOW
    owned_follow.last_notified_at = NOW
    doc = source_and_doc(session, title="主文件")
    twin = Document(
        source_id=doc.source_id,
        url=f"{doc.source.url}copy.html",
        url_key="data-copy",
        title="副本标题",
        publish_date=doc.publish_date,
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    session.add(twin)
    session.flush()
    session.add(
        DocumentMerge(duplicate_id=twin.id, canonical_id=doc.id, active=True, match_key="data")
    )
    own_feedback = Feedback(
        user_id=owner.id,
        document_id=twin.id,
        content="主文件有误",
        check_result="pass",
        created_at=NOW,
    )
    own_subscription = Subscription(
        user_id=owner.id,
        kind="newDocs",
        template_id="n",
        follow_id=follow_id,
        status="sent",
        created_at=NOW,
        attempted_at=NOW,
    )
    session.add_all(
        [
            own_feedback,
            own_subscription,
            Event(user_id=owner.id, name="search", props={}, created_at=NOW),
        ]
    )
    session.commit()
    settings(monkeypatch, openid="other")
    agree(client)
    other = session.scalar(select(User).where(User.openid == "other"))
    session.add_all(
        [
            Feedback(user_id=other.id, content="别人的纠错", check_result="unchecked"),
            Event(user_id=other.id, name="search", props={"count": 1}),
        ]
    )
    session.commit()

    settings(monkeypatch, version="updated")
    response = client.get("/api/v1/me/data")
    assert response.status_code == 200
    data = response.json()
    assert data["user"] == {
        "openid": "dev-user",
        "createdAt": owner.created_at.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(),
        "lastVisitedAt": owner.last_visited_at.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(),
        "agreed": {"version": "2026-09", "at": NOW.isoformat()},
    }
    assert data["follows"] == [
        {
            "id": follow_id,
            "name": "科技项目",
            "conditions": {"kind": "all", "time": "不限"},
            "createdAt": owned_follow.created_at.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(),
            "lastSeenAt": NOW.isoformat(),
            "lastNotifiedAt": NOW.isoformat(),
        }
    ]
    assert data["subscriptionsTotal"] == data["feedbackTotal"] == data["eventsTotal"] == 1
    assert data["subscriptions"] == [
        {
            "id": own_subscription.id,
            "kind": "newDocs",
            "followId": follow_id,
            "docId": None,
            "docTitle": None,
            "status": "sent",
            "createdAt": NOW.isoformat(),
            "attemptedAt": NOW.isoformat(),
        }
    ]
    assert data["feedback"] == [
        {
            "id": own_feedback.id,
            "docId": twin.id,
            "docTitle": "主文件",
            "content": "主文件有误",
            "checkResult": "pass",
            "createdAt": NOW.isoformat(),
        }
    ]
    assert data["events"] == [{"name": "search", "props": {}, "createdAt": NOW.isoformat()}]
    assert data["exportedAt"] == NOW.isoformat()


def test_personal_data_caps_and_size(client, session, monkeypatch):
    settings(monkeypatch)
    agree(client)
    owner = session.scalar(select(User).where(User.openid == "dev-user"))
    follow_id = follow(client).json()["id"]
    session.add_all(
        Subscription(
            user_id=owner.id,
            kind="newDocs",
            template_id="n",
            follow_id=follow_id,
            status="unused",
            created_at=NOW + timedelta(seconds=i),
        )
        for i in range(501)
    )
    session.add_all(
        Feedback(
            user_id=owner.id,
            content=f"纠错 {i}",
            check_result="unchecked",
            created_at=NOW + timedelta(seconds=i),
        )
        for i in range(201)
    )
    session.add_all(
        Event(user_id=owner.id, name="search", props={}, created_at=NOW + timedelta(seconds=i))
        for i in range(501)
    )
    session.commit()
    response = client.get("/api/v1/me/data")
    assert response.status_code == 200
    data = response.json()
    assert (data["subscriptionsTotal"], data["feedbackTotal"], data["eventsTotal"]) == (
        501,
        201,
        501,
    )
    assert (len(data["subscriptions"]), len(data["feedback"]), len(data["events"])) == (
        500,
        200,
        500,
    )
    assert data["feedback"][0]["content"] == "纠错 200"
    assert data["feedback"][-1]["content"] == "纠错 1"
    assert data["subscriptions"][0]["id"] > data["subscriptions"][-1]["id"]
    assert len(response.content) <= 1000 * 1024


def test_personal_data_large_legacy_event_is_bounded(client, session, monkeypatch):
    settings(monkeypatch)
    agree(client)
    owner = session.scalar(select(User).where(User.openid == "dev-user"))
    session.add(Event(user_id=owner.id, name="search", props={"old": "x" * 1_100_000}))
    session.commit()
    response = client.get("/api/v1/me/data")
    assert response.status_code == 200
    assert len(response.content) <= 1000 * 1024
    assert response.json()["eventsTotal"] == 1
    assert response.json()["events"] == []


def test_personal_data_deleted_document_has_no_title(client, session, monkeypatch):
    settings(monkeypatch)
    agree(client)
    doc = source_and_doc(session)
    feedback_id = client.post(
        "/api/v1/feedback", json={"docId": doc.id, "content": "标题有误"}
    ).json()["id"]
    session.delete(doc)
    session.commit()
    item = client.get("/api/v1/me/data").json()["feedback"][0]
    assert item["id"] == feedback_id
    assert item["docId"] is None
    assert item["docTitle"] is None


def test_rename_follow_and_delete_feedback(client, session, monkeypatch):
    settings(monkeypatch)
    agree(client)
    follow_id = follow(client).json()["id"]
    feedback_id = client.post("/api/v1/feedback", json={"content": "有误"}).json()["id"]
    path = f"/api/v1/me/follows/{follow_id}"
    renamed = client.put(path, json={"name": "  新名称  "})
    assert renamed.status_code == 200
    assert renamed.json() == {
        "id": follow_id,
        "name": "新名称",
        "conditions": {"kind": "all", "time": "不限"},
        "newCount": 0,
    }
    assert client.put(path, json={"name": "  "}).status_code == 422
    assert client.put(path, json={"name": "字" * 41}).status_code == 422
    assert client.put(path, json={"name": "  " + "字" * 40 + "  "}).status_code == 200
    assert client.put("/api/v1/me/follows/99999", json={"name": "x"}).status_code == 404
    assert client.delete("/api/v1/me/feedback/99999").status_code == 404
    settings(monkeypatch, openid="other")
    agree(client)
    assert client.put(path, json={"name": "别人的"}).status_code == 404
    assert client.delete(f"/api/v1/me/feedback/{feedback_id}").status_code == 404
    # 协议更新后还没重新同意：更正（改名）和删除一样照常可以
    settings(monkeypatch, version="updated")
    renamed_old = client.put(path, json={"name": "旧版也能改"})
    assert renamed_old.status_code == 200
    assert renamed_old.json()["name"] == "旧版也能改"
    assert client.delete(f"/api/v1/me/feedback/{feedback_id}").status_code == 204
    assert client.delete(f"/api/v1/me/feedback/{feedback_id}").status_code == 404
    assert session.get(Feedback, feedback_id) is None


def test_feedback_masks_personal_information_before_check_and_save(client, session, monkeypatch):
    settings(monkeypatch, trust=True)
    headers = {"X-WX-SOURCE": "mini", "X-WX-OPENID": "wx"}
    agree(client, headers)
    sent = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"errcode": 0, "result": {"suggest": "pass"}}

    def checked(url, *, json, timeout):
        assert url == "http://api.weixin.qq.com/wxa/msg_sec_check"
        assert json["openid"] == "wx"
        assert timeout == 5
        sent.append(json["content"])
        return Response()

    monkeypatch.setattr(me.httpx, "post", checked)
    cases = [
        ("13812345678", "［已隐去］", True),
        ("138 1234 5678", "［已隐去］", True),
        ("138-1234-5678", "［已隐去］", True),
        ("11010519491231002X", "［已隐去］", True),
        ("110105194912310021", "110105194912310021", False),
        ("913502001234567890", "913502001234567890", False),
        ("test.user+tag@example.com", "［已隐去］", True),
        ("a13812345678b 与 913502001234567890", "a［已隐去］b 与 913502001234567890", True),
        ("  ", "  ", False),
    ]
    for raw, expected, masked in cases:
        response = client.post("/api/v1/feedback", json={"content": raw}, headers=headers)
        assert response.status_code == 201
        assert response.json()["masked"] is masked
        assert sent[-1] == expected
        assert session.get(Feedback, response.json()["id"]).content == expected


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
        agreement_version="2026-09",
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
    config = SimpleNamespace(
        send_wx_messages=True,
        template_deadline=None,
        template_newdocs=None,
        agreement_version="2026-09",
    )
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
    config = SimpleNamespace(
        send_wx_messages=True,
        template_deadline=None,
        template_newdocs=None,
        agreement_version="2026-09",
    )
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
        retain_events_days=90,
        retain_feedback_days=180,
        retain_used_subscriptions_days=30,
        retain_unused_subscriptions_days=365,
    )
    assert run_cleanup(session, settings=config, stamp=NOW) == {
        "events": 1,
        "feedback": 1,
        "subscriptions": 1,
        "unused_subscriptions": 0,
    }
    assert session.scalar(select(func.count(Event.id))) == 1
    assert [f.content for f in session.scalars(select(Feedback))] == ["新的"]
    assert [s.status for s in session.scalars(select(Subscription))] == ["unused"]
    assert session.scalar(select(func.count(Follow.id))) == 1


def test_agreement_version_must_be_current(client, session, monkeypatch):
    """只接受同意当前版本；协议更新后旧版的同意失效，要重新同意。"""
    settings(monkeypatch)
    assert client.post("/api/v1/me/agreement", json={"version": "2025-01"}).status_code == 422
    agree(client)
    assert client.get("/api/v1/me/follows").status_code == 200
    settings(monkeypatch, version="2026-12")  # 协议更新
    response = client.get("/api/v1/me/follows")
    assert response.status_code == 403 and "已更新" in response.json()["detail"]
    ok = client.post("/api/v1/me/agreement", json={"version": "2026-12"})
    assert ok.status_code == 200
    assert client.get("/api/v1/me/follows").status_code == 200


def test_notify_skips_users_on_old_agreement(client, session, monkeypatch):
    """协议更新后还没重新同意的用户先不发提醒，授权保留。"""
    settings(monkeypatch)
    agree(client)
    doc = source_and_doc(session, due=NOW.date() + timedelta(days=1))
    created = client.post(
        "/api/v1/me/subscriptions",
        json={"kind": "deadline", "templateId": "d", "docId": doc.id},
    )
    assert created.status_code == 201
    template = {"id": "d", "fields": {"thing1": "title", "date2": "date"}}
    config = SimpleNamespace(
        send_wx_messages=True,
        template_deadline=template,
        template_newdocs=None,
        agreement_version="2026-12",  # 协议已更新，用户同意的是 2026-09
    )

    def forbidden(_payload):
        raise AssertionError("不应发送")

    result = run_notify(session, settings=config, sender=forbidden, stamp=NOW)
    assert result["skipped"] == 1 and result["sent"] == 0
    assert session.scalar(select(Subscription.status)) == "unused"


def test_deadline_subscription_only_for_public_documents(client, session, monkeypatch):
    """只有 2 级来源（只存题录）、没有 1 级副本的文件，不能订阅它的截止提醒。"""
    settings(monkeypatch)
    agree(client)
    doc = source_and_doc(session, due=NOW.date() + timedelta(days=5))
    body = {"kind": "deadline", "templateId": "d", "docId": doc.id}
    source = session.get(Source, doc.source_id)
    source.rights_level = 2
    session.commit()
    assert client.post("/api/v1/me/subscriptions", json=body).status_code == 422
    source.rights_level = 1
    session.commit()
    assert client.post("/api/v1/me/subscriptions", json=body).status_code == 201


def test_deadline_reminder_rechecks_publicity_when_sending(client, session, monkeypatch):
    """订阅之后来源改成 2 级（只存题录）：到时不发，订阅记为失效。"""
    settings(monkeypatch)
    agree(client)
    doc = source_and_doc(session, due=NOW.date() + timedelta(days=1))
    sub_id = client.post(
        "/api/v1/me/subscriptions", json={"kind": "deadline", "templateId": "d", "docId": doc.id}
    ).json()["id"]
    session.get(Source, doc.source_id).rights_level = 2
    session.commit()
    config = SimpleNamespace(
        send_wx_messages=True,
        template_deadline={
            "id": "d",
            "fields": {"title": "thing1", "date": "date2", "kind": "thing3"},
        },
        template_newdocs=None,
        agreement_version="2026-09",
    )

    def forbidden(_):
        raise AssertionError("不能公开的文件不发提醒")

    assert run_notify(session, settings=config, sender=forbidden, stamp=NOW)["expired"] == 1
    assert session.get(Subscription, sub_id).status == "expired"
