"""小程序"本期清单"公开接口：只给已发布的周报，撤下或不再公开的条目不返回（PR #287）。"""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_weekly import NOW, START, document, generate
from tests.test_weekly import world as world  # noqa: F401 - 夹具
from youju import weekly
from youju.api import app as app_module
from youju.api import digest as digest_api
from youju.api import v1
from youju.db import get_session
from youju.models import DigestIssue, DigestProfile, Document, DocumentMerge
from youju.takedown import change_state


@pytest.fixture
def profile(session):
    p = DigestProfile(name="全国版")
    session.add(p)
    session.commit()
    return p


@pytest.fixture
def client(session_factory, monkeypatch):
    monkeypatch.setattr(v1, "now", lambda: NOW)
    digest_api._CACHE.clear()

    def override():
        with session_factory() as s:
            yield s

    app_module.app.dependency_overrides[get_session] = override
    with TestClient(app_module.app) as c:
        yield c
    app_module.app.dependency_overrides.clear()
    digest_api._CACHE.clear()


def published(session, profile):
    issue = generate(session, profile).issue
    issue.status = "published"
    session.commit()
    return issue


def titles(body):
    return [item["title"] for item in body["items"]]


def test_published_weekly_lists_every_entry_grouped_like_article(client, session, profile, world):
    issue = generate(session, profile).issue
    path = f"/api/v1/digests/{issue.id}"
    # 草稿不对外：管理台标为已发布以后才能打开
    assert client.get(path).status_code == 404
    issue.status = "published"
    session.commit()
    body = client.get(path).json()
    assert (body["id"], body["title"], body["weekStart"], body["weekEnd"]) == (
        issue.id,
        issue.title,
        "2026-09-28",
        "2026-10-04",
    )
    assert body["counts"] == issue.counts and body["dataAt"].startswith("2026-10-04T23:30")
    # 正文里浙江只列 5 条，清单里 8 条都有；国家部委在前，块内先截止再新通知、名单、政策
    assert body["provinces"] == [
        {"province": "国家", "name": "国家部委", "count": 1},
        {"province": "浙江", "name": "浙江", "count": 8},
    ]
    assert titles(body) == [
        "国家项目申报通知",
        "历史项目申报通知",
        "项目申报通知",
        "边界项目申报通知",
        "待核对项目申报通知",
        "正文变化项目申报通知",
        "企业名单公示",
        "企业名单",
        "管理办法",
    ]
    assert [item["section"] for item in body["items"]] == [
        "applications",
        "deadlines",
        "deadlines",
        "deadlines",
        "applications",
        "applications",
        "lists",
        "lists",
        "policies",
    ]
    assert (body["total"], body["truncated"], body["hidden"]) == (9, False, 0)
    first = body["items"][2]
    # 和小程序列表行同一格式：doc-row 直接用；截止只给核对通过的
    assert first["id"] == str(world["apply"].id)
    assert first["deadline"] == {"date": "2026-10-10", "time": "", "verified": True}
    assert first["deadline_state"] == "verified"
    assert first["url"] == world["apply"].url
    pending = body["items"][4]
    assert pending["deadline"] is None and pending["deadline_state"] == "unverified"
    assert "不可输出" not in str(body)


def test_takedown_private_source_and_merge_after_publish(client, session, profile, world):
    issue = published(session, profile)
    path = f"/api/v1/digests/{issue.id}"
    assert client.get(path).json()["total"] == 9
    # 在管理台下架副本，整组不再公开：立即不返回（缓存按下架记录失效），只给条数
    change_state(
        session, world["twin"].id, action="takedown", note="测试", operator="测试", reason="其他"
    )
    body = client.get(path).json()
    assert "项目申报通知" not in titles(body) and body["hidden"] == 1
    assert body["provinces"][1] == {"province": "浙江", "name": "浙江", "count": 7}
    # 来源不再是一级来源：这一来源的条目都不返回
    world["src"].rights_level = 2
    session.commit()
    digest_api._CACHE.clear()  # 来源公开范围变化交给 60 秒缓存兜底
    body = client.get(path).json()
    assert titles(body) == ["国家项目申报通知"] and body["hidden"] == 8
    world["src"].rights_level = 1
    session.commit()
    change_state(session, world["twin"].id, action="restore", note="测试", operator="测试")
    # 发布以后，清单里的文件成了另一份文件的副本：按现在的主文件列一次
    master = document(session, world["src"], "新的主文件", START)
    session.add(
        DocumentMerge(
            duplicate_id=world["nat"].id, canonical_id=master.id, match_key="later", active=True
        )
    )
    session.commit()
    digest_api._CACHE.clear()
    body = client.get(path).json()
    assert "国家项目申报通知" not in titles(body) and "新的主文件" in titles(body)
    assert body["provinces"][0]["province"] == "浙江" and body["hidden"] == 0


def test_deadlines_shown_live_after_publish(client, session, profile, world):
    issue = published(session, profile)
    path = f"/api/v1/digests/{issue.id}"
    row = session.scalars(
        select(weekly.DocumentDeadline).where(
            weekly.DocumentDeadline.document_id == world["twin"].id
        )
    ).one()
    row.approved_date = date(2026, 10, 20)
    session.commit()
    body = client.get(path).json()
    item = next(item for item in body["items"] if item["title"] == "项目申报通知")
    # 发布后改了核对过的日期：显示新的（缓存按截止版本失效）；栏目仍按发布时的
    assert item["deadline"]["date"] == "2026-10-20" and item["section"] == "deadlines"
    row.status, row.approved_date = "pending", None
    session.commit()
    body = client.get(path).json()
    item = next(item for item in body["items"] if item["title"] == "项目申报通知")
    assert item["deadline"] is None and item["deadline_state"] == "unverified"


def test_only_published_weekly_issues_and_retraction(client, session, profile, world):
    issue = published(session, profile)
    reminder = weekly.generate(session, profile.id, None, NOW, "reminder").issue
    listing = weekly.generate(
        session, profile.id, None, NOW, "list_flash", subject_id=world["listing"].id
    ).issue
    for other in (reminder, listing):
        other.status = "published"
    session.commit()
    for other in (reminder, listing):
        assert client.get(f"/api/v1/digests/{other.id}").status_code == 404
    assert client.get("/api/v1/digests/999").json()["detail"] == "这一期周报不存在，或已撤回"
    assert client.get(f"/api/v1/digests/{issue.id}").status_code == 200
    issue.status = "draft"
    session.commit()
    assert client.get(f"/api/v1/digests/{issue.id}").status_code == 404  # 撤回立即不对外
    assert client.get("/api/v1/digests/abc").status_code == 422


def test_latest_is_newest_published_national_weekly(client, session, profile, world):
    assert client.get("/api/v1/digests/latest").json()["detail"] == "还没有发布的周报"
    issue = generate(session, profile).issue
    assert client.get("/api/v1/digests/latest").status_code == 404  # 草稿不算
    issue.status = "published"
    session.commit()
    assert client.get("/api/v1/digests/latest").json() == {
        "id": issue.id,
        "title": issue.title,
        "weekStart": "2026-09-28",
        "weekEnd": "2026-10-04",
    }
    # 地区版、关键词版的周报更新，"本周周报"也只给全国版
    regional = DigestProfile(name="浙江版", provinces=["浙江"])
    keyword = DigestProfile(name="关键词版", keywords=["项目"])
    session.add_all([regional, keyword])
    session.commit()
    later = START + timedelta(days=7)
    document(session, world["src"], "下一周的通知", later)
    session.commit()
    for other in (regional, keyword):
        weekly.generate(
            session, other.id, later, NOW + timedelta(days=7)
        ).issue.status = "published"
    session.commit()
    assert client.get("/api/v1/digests/latest").json()["id"] == issue.id
    newer = weekly.generate(session, profile.id, later, NOW + timedelta(days=7)).issue
    newer.status = "published"
    session.commit()
    assert client.get("/api/v1/digests/latest").json()["id"] == newer.id


def test_legacy_issue_without_items_uses_article_documents(client, session, profile, world):
    issue = published(session, profile)
    issue.items = None
    session.commit()
    body = client.get(f"/api/v1/digests/{issue.id}").json()
    # 本功能上线前的期刊：只有正文里出现的文件，栏目按现在的类型
    assert body["total"] == len(issue.document_ids) == 6
    assert {item["section"] for item in body["items"]} == {"applications"}


def test_item_cap_and_truncated_flag(client, session, profile, world, monkeypatch):
    monkeypatch.setattr(digest_api, "MAX_ITEMS", 3)
    issue = published(session, profile)
    body = client.get(f"/api/v1/digests/{issue.id}").json()
    assert len(body["items"]) == 3 and body["total"] == 9 and body["truncated"] is True
    assert sum(item["count"] for item in body["provinces"]) == 9


def test_digests_readable_on_admin_through_cloud_calls_only(client, session, profile, world):
    """公众号文章点进来的第一屏：admin 常驻服务也放行（只认经云调用来的 GET），不等冷启动。"""
    issue = published(session, profile)
    settings = {"role": "admin", "admin_token": "t" * 32}
    original = app_module.get_settings
    app_module.get_settings = lambda: SimpleNamespace(**settings)
    try:
        wx = {"X-WX-SOURCE": "wx_client"}
        for path in (f"/api/v1/digests/{issue.id}", "/api/v1/digests/latest"):
            assert client.get(path).json()["detail"] == "Not Found"  # 公网直接访问
            assert client.get(path, headers=wx).status_code == 200, path
        assert client.get(f"/api/v1/digests/{issue.id}/x", headers=wx).status_code == 404
        assert client.post("/api/v1/digests/latest", headers=wx).status_code == 404
    finally:
        app_module.get_settings = original


def test_items_column_never_holds_names(session, profile, world):
    issue = generate(session, profile).issue
    assert all(set(item) == {"id", "section", "province"} for item in issue.items)
    assert session.scalars(select(DigestIssue)).one().items == issue.items
    assert {item["id"] for item in issue.items} <= set(session.scalars(select(Document.id)))
