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
from youju.config import Settings
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
    monkeypatch,
    *,
    trust=False,
    openid="dev-user",
    deadline=None,
    newdocs=None,
    version="2026-09",
    oldest="",
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
            agreement_min_version=oldest,
            retain_events_days=90,
            retain_feedback_days=180,
            retain_used_subscriptions_days=30,
            retain_unused_subscriptions_days=365,
        ),
    )


def agree(client, headers=None):
    """同意用户协议和隐私保护指引（之后才能关注、订阅、纠错）。"""
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
    settings(monkeypatch)
    assert client.get("/api/v1/me").json()["agreed"] is None
    # 同意之前：只查看不建用户记录；关注、纠错都不收
    assert session.scalar(select(func.count(User.id))) == 0
    assert client.post("/api/v1/feedback", json={"content": "纠错"}).status_code == 403
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


def test_feedback_security(client, session, monkeypatch):
    settings(monkeypatch)
    agree(client)
    assert client.post("/api/v1/feedback", json={"content": ""}).status_code == 422
    assert client.post("/api/v1/feedback", json={"content": "a" * 501}).status_code == 422
    assert (
        client.post("/api/v1/feedback", json={"docId": 999, "content": "有错"}).status_code == 422
    )
    assert client.post("/api/v1/feedback", json={"content": "请核对"}).status_code == 201
    assert session.scalar(select(Feedback.check_result)) == "unchecked"
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


def test_event_endpoint_is_gone(client, session, monkeypatch):
    """写使用记录（埋点）的接口已删（个人信息保护影响评估第六节（二）第 13 条）：已同意的用户
    自己构造请求也写不进去。以前留下的照常导出、到期清理、注销时删除（见下面几个测试）。"""
    settings(monkeypatch)
    agree(client)
    for body in ({"name": "search"}, {"name": "search", "props": {"count": 1}}):
        assert client.post("/api/v1/events", json=body).status_code == 404
    assert session.scalar(select(func.count(Event.id))) == 0


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
    # 写使用记录的接口已删：直接写库模拟以前留下的一条，注销时随用户删除
    session.add(Event(user_id=session.scalar(select(User.id)), name="feedback", props={}))
    session.commit()
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
        "deadlineClues": [],
        "deadlineCluesTotal": 0,
        "events": [],
        "eventsTotal": 0,
        "aiChats": [],
        "aiChatsTotal": 0,
        "aiChatTurnsTotal": 0,
        "boostTrial": None,
        "passGifts": [],
        "passGiftsTotal": 0,
        "retention": {
            "events": 90,
            "feedback": 180,
            "usedSubscriptions": 30,
            "unusedSubscriptions": 365,
            "toolInput": 7,
            "toolTasks": 30,
            "aiChats": 30,
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
            "handled": False,
            "handledAt": None,
            "outcome": None,
            "outcomeLabel": None,
            "reply": None,
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


@pytest.mark.parametrize("area_kind", ["city", "district"])
@pytest.mark.parametrize("field,value", [("enabled", False), ("last_success_at", None)])
def test_unavailable_city_follow_remains_visible_and_home_keeps_valid_counts(
    client, session, monkeypatch, field, value, area_kind
):
    settings(monkeypatch)
    agree(client)
    city_doc = source_and_doc(session, title="合成青岛通知")
    city_source = session.get(Source, city_doc.source_id)
    city_source.province, city_source.city, city_source.level = "山东", "青岛市", "市级"
    city_source.last_success_at = NOW
    area = {"cities": "山东/青岛"}
    if area_kind == "district":
        city_source.level, city_source.district = "区县级", "市南区"
        area = {"districts": "山东/青岛/市南区"}
    session.commit()
    city_follow = follow(client, {**area, "time": "不限"})
    assert city_follow.status_code == 201
    city_id = city_follow.json()["id"]
    source_and_doc(session, title="合成北京通知")
    valid = follow(client, {"regions": "北京", "time": "不限"})
    assert valid.status_code == 201
    setattr(city_source, field, value)
    session.commit()

    response = client.get("/api/v1/me/follows")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["newTotal"] is None
    assert body["availableNewTotal"] == 1 and body["unavailableCount"] == 1
    unavailable, available = body["items"]
    assert unavailable["id"] == city_id and unavailable["newCount"] is None
    assert unavailable["queryUnavailable"] is True
    assert unavailable["queryMessage"] == me.FOLLOW_QUERY_UNAVAILABLE
    assert unavailable["conditions"] == city_follow.json()["conditions"]
    assert available["newCount"] == 1 and "queryUnavailable" not in available
    home = client.get("/api/v1/home")
    assert home.status_code == 200, home.text
    stats = home.json()["stats"]
    assert stats["followed_new"] is None and stats["followed_available_new"] == 1
    assert stats["followed_unavailable"] == 1
    assert stats["followed_message"] == me.FOLLOW_QUERY_UNAVAILABLE
    assert client.get(f"/api/v1/me/follows/{city_id}/new").status_code == 422

    # 恢复来源后原关注自动可查；不改存储条件，也不把暂时不可用伪装成零条。
    setattr(city_source, field, True if field == "enabled" else NOW)
    session.commit()
    restored = client.get("/api/v1/me/follows").json()
    assert restored["newTotal"] == 2 and "unavailableCount" not in restored
    assert all("queryUnavailable" not in item for item in restored["items"])
    assert client.get("/api/v1/home").json()["stats"]["followed_new"] == 2
    setattr(city_source, field, value)
    session.commit()
    assert client.delete(f"/api/v1/me/follows/{city_id}").status_code == 204
    assert client.get("/api/v1/me/follows").json()["newTotal"] == 1


@pytest.mark.parametrize("target", ["_follow_out", "followed_week_stats", "list_follows"])
@pytest.mark.parametrize("failure", ["database", "http-503"])
def test_follow_views_do_not_hide_infrastructure_failure(
    client, session, monkeypatch, target, failure
):
    from fastapi import HTTPException
    from sqlalchemy.exc import OperationalError

    settings(monkeypatch)
    agree(client)
    item = follow(client)
    assert item.status_code == 201
    saved = session.get(Follow, item.json()["id"])
    user = session.get(User, saved.user_id)
    error = (
        OperationalError("synthetic query", {}, Exception("synthetic database unavailable"))
        if failure == "database"
        else HTTPException(status_code=503, detail="synthetic unavailable")
    )

    def unavailable(*args):
        raise error

    monkeypatch.setattr(me, "_conditions_stmt", unavailable)
    with pytest.raises(type(error)) as caught:
        getattr(me, target)(session, saved if target == "_follow_out" else user)
    assert caught.value is error


@pytest.mark.parametrize("area_kind", ["city", "district"])
@pytest.mark.parametrize("send", [False, True])
def test_notify_unavailable_city_keeps_authorization_and_continues(
    client, session, monkeypatch, caplog, send, area_kind
):
    settings(monkeypatch)
    agree(client)
    city_doc = source_and_doc(session, title="合成青岛项目通知")
    city_source = session.get(Source, city_doc.source_id)
    city_source.province, city_source.city, city_source.level = "山东", "青岛市", "市级"
    city_source.last_success_at = NOW
    area = {"cities": "山东/青岛"}
    if area_kind == "district":
        city_source.level, city_source.district = "区县级", "市南区"
        area = {"districts": "山东/青岛/市南区"}
    session.commit()
    city_follow = follow(client, {**area, "time": "不限"})
    assert city_follow.status_code == 201, city_follow.text
    source_and_doc(session, title="合成北京项目通知")
    valid_follow = follow(client, {"regions": "北京", "time": "不限"})
    assert valid_follow.status_code == 201
    subscriptions = []
    for item in (city_follow, valid_follow):
        created = client.post(
            "/api/v1/me/subscriptions",
            json={"kind": "newDocs", "templateId": "n", "followId": item.json()["id"]},
        )
        assert created.status_code == 201, created.text
        subscriptions.append(created.json()["id"])
    # 保存关注时合法；之后来源停用，第一条输入失效不能影响第二条通知。
    city_source.enabled = False
    session.commit()
    config = SimpleNamespace(
        send_wx_messages=send,
        template_newdocs={"id": "n", "fields": {"thing1": "title", "number2": "count"}},
        template_deadline=None,
        agreement_version="2026-09",
    )
    payloads = []

    def sender(payload):
        payloads.append(payload)
        return {"errcode": 0}

    result = run_notify(session, settings=config, sender=sender, stamp=NOW)
    assert result["skipped"] == 1
    assert result["sent" if send else "dry_run"] == 1
    skipped = session.get(Subscription, subscriptions[0])
    assert skipped.status == "unused" and skipped.attempted_at is None and skipped.errcode is None
    assert session.get(Follow, city_follow.json()["id"]).last_notified_at is None
    assert session.get(Subscription, subscriptions[1]).status == ("sent" if send else "unused")
    assert "关注筛选条件已不可用（HTTP 422）" in caplog.text
    if send:
        assert len(payloads) == 1
        assert payloads[0]["data"] == {
            "thing1": {"value": "合成北京项目通知"},
            "number2": {"value": "1"},
        }
    else:
        assert payloads == []


@pytest.mark.parametrize("failure", ["database", "http-503"])
def test_notify_does_not_hide_infrastructure_failure(client, session, monkeypatch, failure):
    from fastapi import HTTPException
    from sqlalchemy.exc import OperationalError

    from youju import notify

    settings(monkeypatch)
    agree(client)
    follow_id = follow(client).json()["id"]
    created = client.post(
        "/api/v1/me/subscriptions",
        json={"kind": "newDocs", "templateId": "n", "followId": follow_id},
    )
    assert created.status_code == 201
    error = (
        OperationalError("synthetic query", {}, Exception("synthetic database unavailable"))
        if failure == "database"
        else HTTPException(status_code=503, detail="synthetic unavailable")
    )

    def unavailable(*args):
        raise error

    monkeypatch.setattr(notify, "notification_statement", unavailable)
    with pytest.raises(type(error)) as caught:
        run_notify(session, settings=SimpleNamespace(send_wx_messages=True), stamp=NOW)
    assert caught.value is error
    row = session.get(Subscription, created.json()["id"])
    assert row.status == "unused" and row.attempted_at is None


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
        "job_runs": 0,
        "events": 1,
        "feedback": 1,
        "subscriptions": 1,
        "unused_subscriptions": 0,
        "advisor_input_rows": 0,
        "pay_events": 0,
        "advisor_files": 0,
        "advisor_ai_tasks": 0,
        "advisor_data_tasks": 0,
        "advisor_usage_rows": 0,
        "ai_safety_hits": 0,
        "ai_results_removed": 0,
        "ai_chat_turns": 0,
        "ai_chats": 0,
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


def test_agreement_min_version_keeps_old_agreements(client, session, monkeypatch):
    """协议升版只给新功能加了条款时（如 AI 版）：仍认升版前的版本——旧版小程序照旧能同意旧版、用关注；
    新功能只认当前版本；已同意过新版本的不会被旧版小程序改回旧版本（Codex 审查）。"""
    settings(monkeypatch, version="2026-12", oldest=" 2026-09 ")
    agree(client)  # 旧版小程序同意的是 2026-09
    assert client.get("/api/v1/me/follows").status_code == 200
    assert client.get("/api/v1/me").json()["agreed"]["version"] == "2026-09"
    # 早于仍认的最早版本、晚于当前版本的都不收；提示更新小程序
    for version in ("2025-01", "2027-01"):
        response = client.post("/api/v1/me/agreement", json={"version": version})
        assert response.status_code == 422 and "重新打开" in response.json()["detail"]
    # 新功能要求同意过写进它的条款的那一版（AI 分支里固定为 AI 条款的版本，回退配置也不会放宽）
    user = session.scalar(select(User).where(User.openid == "dev-user"))
    assert not me.agreement_at_least(user.agreement_version, "2026-12")
    # 新版小程序请用户重新同意当前版本后，新功能也能用
    assert client.post("/api/v1/me/agreement", json={"version": "2026-12"}).status_code == 200
    session.expire_all()
    user = session.scalar(select(User).where(User.openid == "dev-user"))
    assert me.agreement_at_least(user.agreement_version, "2026-12")
    agreed_at = user.agreement_at
    # 另一台手机上的旧版小程序又提交了 2026-09：保留 2026-12 那次同意，版本和时间都不动
    response = client.post("/api/v1/me/agreement", json={"version": "2026-09"})
    assert response.status_code == 200 and response.json()["version"] == "2026-12"
    session.expire_all()
    user = session.scalar(select(User).where(User.openid == "dev-user"))
    assert (user.agreement_version, user.agreement_at) == ("2026-12", agreed_at)
    assert client.get("/api/v1/me").json()["agreed"]["valid"] is True


def test_agreement_version_must_look_like_year_month(client, session, monkeypatch):
    """版本号按"年-月"开头比较：写法不对的同意一律不算；重新同意时也不当成"更新的版本"留着。"""
    settings(monkeypatch)
    agree(client)
    user = session.scalar(select(User).where(User.openid == "dev-user"))
    user.agreement_version = "old"  # 字母排在数字后面，直接比字符串会被当成"更新"
    session.commit()
    assert client.get("/api/v1/me/follows").status_code == 403
    agree(client)
    session.expire_all()
    stored = session.scalar(select(User.agreement_version).where(User.openid == "dev-user"))
    assert stored == "2026-09"
    assert client.get("/api/v1/me/follows").status_code == 200


def test_agreement_accepts_only_well_formed_versions(client, monkeypatch):
    """同意接口只收完整写法的版本号（年-月，可带 .数字）：在仍认的范围里、写法不对的也不收（D-098）。"""
    settings(monkeypatch, version="2026-12", oldest="2026-10.2")
    for version in ("2026-11.0x", "2026-11.1 ", "2026-11-1", "2026-11.", "2026-11.1.2"):
        response = client.post("/api/v1/me/agreement", json={"version": version})
        assert response.status_code == 422, version
    for version in ("2026-10.2", "2026-11", "2026-11.1", "2026-12"):
        assert client.post("/api/v1/me/agreement", json={"version": version}).status_code == 200, (
            version
        )


def test_first_release_agreement_without_ai_terms(client, session, monkeypatch):
    """D-093 ⑤：过审版（首发 16 页）带不含 AI 条款的协议 2026-10.2，完整版带 2026-11。后台当前版本仍是
    2026-11、最早认 2026-10.2：两种包都能同意、都能用关注；只同意过 2026-10.2 的人没同意过 AI 和批量工具的
    条款，AI（对所有人开放时）和批量工具都要先重新同意 2026-11；10/15 首发的旧 2026-10 正文不再认。"""
    from youju.ai import interpret

    settings(monkeypatch, version="2026-11", oldest="2026-10.2")
    response = client.post("/api/v1/me/agreement", json={"version": "2026-10.2"})
    assert response.status_code == 200 and response.json()["valid"] is True
    assert client.get("/api/v1/me/follows").status_code == 200
    user = session.scalar(select(User).where(User.openid == "dev-user"))
    assert not me.tools_agreed(user.agreement_version)
    open_to_all = SimpleNamespace(ai_advisor_audience="all")
    assert not interpret.ai_agreed(open_to_all, user)
    response = client.post("/api/v1/me/agreement", json={"version": "2026-10"})
    assert response.status_code == 422
    # 完整版请用户读完 2026-11 再同意：批量工具的条款覆盖到了；AI 10/28 起要 2026-11.1 或更新的版本
    # （完整版 2026-11 没写"AI 结果送微信内容安全检查"，完整版升 2026-12 时补上）
    assert client.post("/api/v1/me/agreement", json={"version": "2026-11"}).status_code == 200
    session.expire_all()
    user = session.scalar(select(User).where(User.openid == "dev-user"))
    assert me.tools_agreed(user.agreement_version)
    assert not interpret.ai_agreed(open_to_all, user)
    user.agreement_version = "2026-11.1"
    assert interpret.ai_agreed(open_to_all, user)


def test_agreement_min_version_raised_requires_update(client, monkeypatch):
    """调高仍认的最早版本（旧条款不再覆盖旧功能）后：停在旧版本的用户要更新小程序、重新同意。"""
    settings(monkeypatch, version="2026-12", oldest="2026-09")
    agree(client)
    assert client.get("/api/v1/me/follows").status_code == 200
    settings(monkeypatch, version="2026-12")
    response = client.get("/api/v1/me/follows")
    assert response.status_code == 403 and "已更新" in response.json()["detail"]
    # /me 写明这份同意已不算数：版本号和旧版小程序的一样，小程序也会请用户重新同意
    agreed = client.get("/api/v1/me").json()["agreed"]
    assert (agreed["version"], agreed["valid"]) == ("2026-09", False)
    response = client.post("/api/v1/me/agreement", json={"version": "2026-09"})
    assert response.status_code == 422 and "更新到新版本" in response.json()["detail"]


@pytest.mark.parametrize(
    ("version", "oldest"), [("2026-10", "2026-11"), ("v2", ""), ("2026-10", "old")]
)
def test_agreement_version_settings_checked_at_startup(version, oldest):
    """协议版本写法不对、最早版本比当前版本还新：配错就不启动，不会悄悄放宽或卡住所有人。"""
    with pytest.raises(ValueError, match="AGREEMENT"):
        Settings(_env_file=None, agreement_version=version, agreement_min_version=oldest)
    assert Settings(_env_file=None, agreement_version="2026-11", agreement_min_version="2026-10")


def test_notify_sends_to_users_on_still_accepted_version(client, session, monkeypatch):
    """仍认的旧版本：提醒照常发。"""
    settings(monkeypatch)
    agree(client)
    doc = source_and_doc(session, due=NOW.date() + timedelta(days=1))
    created = client.post(
        "/api/v1/me/subscriptions",
        json={"kind": "deadline", "templateId": "d", "docId": doc.id},
    )
    assert created.status_code == 201
    config = SimpleNamespace(
        send_wx_messages=True,
        template_deadline={"id": "d", "fields": {"thing1": "title", "date2": "date"}},
        template_newdocs=None,
        agreement_version="2026-12",
        agreement_min_version="2026-09",
    )
    sent = []

    def sender(payload):
        sent.append(payload)
        return {"errcode": 0}

    result = run_notify(session, settings=config, sender=sender, stamp=NOW)
    assert result["sent"] == 1 and len(sent) == 1


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
        template_deadline={"id": "d", "fields": {"thing1": "title", "date2": "date"}},
        template_newdocs=None,
        agreement_version="2026-09",
    )

    def forbidden(_):
        raise AssertionError("不能公开的文件不发提醒")

    assert run_notify(session, settings=config, sender=forbidden, stamp=NOW)["expired"] == 1
    assert session.get(Subscription, sub_id).status == "expired"


def test_follow_keeps_level_filter(client, session, monkeypatch):
    """关注条件可以带层级；新文件计数按层级筛，层级取值不对时 422。"""
    settings(monkeypatch)
    agree(client)
    assert follow(client, {"time": "不限", "levels": "县级"}).status_code == 422
    national = follow(client, {"time": "不限", "levels": "国家部委"})
    provincial = follow(client, {"time": "不限", "levels": "省级", "regions": "北京"})
    assert national.status_code == 201 and provincial.status_code == 201
    assert provincial.json()["conditions"]["levels"] == "省级"
    source_and_doc(session)  # 北京省级来源的新文件
    counts = {
        row["id"]: row["newCount"] for row in client.get("/api/v1/me/follows").json()["items"]
    }
    assert counts == {national.json()["id"]: 0, provincial.json()["id"]: 1}


def test_follow_saved_with_national_region_still_counts_like_level(client, session, monkeypatch):
    """UI 第二轮起小程序的地区里没有"国家部委"（只在层级里）。之前保存的关注条件地区里还有它：
    服务端照旧接受、照旧计数，和小程序换成层级后的条件新文件数相同；新界面"地区北京 + 层级
    国家部委"保存的就是 regions=国家部委,北京，国家部委和北京的新文件都算。"""
    settings(monkeypatch)
    agree(client)
    made = {
        key: follow(client, {"time": "不限", **conditions}).json()["id"]
        for key, conditions in {
            "old_national": {"regions": "国家部委"},
            "new_national": {"levels": "国家部委"},
            "old_provincial": {"regions": "国家部委,北京", "levels": "省级"},
            "new_provincial": {"regions": "北京", "levels": "省级"},
            "national_and_beijing": {"regions": "国家部委,北京"},
        }.items()
    }
    source_and_doc(session)  # 北京省级来源的新文件
    national = Source(
        level="国家级",
        province="国家",
        dept="科技",
        dept_full_name="科学技术部",
        column_name="通知",
        url="https://example.gov.cn/national/",
        parser_config={},
    )
    session.add(national)
    session.flush()
    session.add(
        Document(
            source_id=national.id,
            url=f"{national.url}a.html",
            url_key=f"{national.id}/a.html",
            title="关于组织申报2026年度重点研发计划项目的通知",
            publish_date=NOW.date(),
            first_seen_at=NOW + timedelta(minutes=1),
            last_seen_at=NOW + timedelta(minutes=1),
        )
    )
    session.commit()
    counts = {
        row["id"]: row["newCount"] for row in client.get("/api/v1/me/follows").json()["items"]
    }
    got = {key: counts[follow_id] for key, follow_id in made.items()}
    assert got == {
        "old_national": 1,
        "new_national": 1,
        "old_provincial": 1,
        "new_provincial": 1,
        "national_and_beijing": 2,
    }


def test_follow_new_folds_guides_when_asked(client, session, monkeypatch):
    """关注的新文件和搜索一样默认折叠资讯与指南（D-051）：写明折叠了几篇，点开照常列出。"""
    settings(monkeypatch)
    agree(client)
    follow_id = follow(client).json()["id"]
    source_and_doc(session)
    source_and_doc(session, title="2026年8月份居民消费价格同比上涨0.8%")
    url = f"/api/v1/me/follows/{follow_id}/new"
    assert client.get(url).json()["total"] == 2  # 旧版小程序不传 fold：和以前一样
    folded = client.get(url, params={"fold": 1}).json()
    assert (folded["total"], folded["folded"], folded["guides"]) == (1, 1, 1)
    assert [item["kind"] for item in folded["items"]] == ["申报通知"]
    shown = client.get(url, params={"fold": 0}).json()
    assert (shown["total"], shown["folded"], shown["guides"]) == (2, 0, 1)
    # 关注条件选了类型的不折叠
    typed = follow(client, {"time": "不限", "types": "申报通知"}).json()["id"]
    body = client.get(f"/api/v1/me/follows/{typed}/new", params={"fold": 1}).json()
    assert (body["total"], body["folded"]) == (1, 0)


def test_profile_task_owner_is_stable_opaque_and_account_scoped(client, monkeypatch):
    settings(monkeypatch, openid="synthetic-owner-one")
    agree(client)
    first = client.get("/api/v1/me").json()
    assert len(first["taskOwner"]) == 64
    assert set(first["taskOwner"]) <= set("0123456789abcdef")
    assert "synthetic-owner-one" not in str(first)
    assert client.get("/api/v1/me").json()["taskOwner"] == first["taskOwner"]
    settings(monkeypatch, openid="synthetic-owner-two")
    agree(client)
    second = client.get("/api/v1/me").json()
    assert second["taskOwner"] != first["taskOwner"]
    assert "synthetic-owner-two" not in str(second)
