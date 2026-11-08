"""D-123：原网页失效的通知 7 天后清理；这 7 天里正文、附件照常能看，可以保存（我的保存）、导出
（youju/gone.py、youju/saved.py、youju/api/saved.py）。开关 YOUJU_GONE_CLEANUP_ENABLED 默认关。"""

import json
import os
from datetime import UTC, date, datetime, timedelta
from io import BytesIO

import pytest
from docx import Document as WordDocument
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from youju import gone, link_check, saved
from youju.api import attachments as download_api
from youju.api import v1
from youju.api.app import app
from youju.config import get_settings
from youju.db import get_session
from youju.models import (
    DeadlineClue,
    Document,
    DocumentDeadline,
    DocumentDetail,
    DocumentMerge,
    DocumentTakedownEvent,
    SavedDocument,
    Source,
    Subscription,
    User,
)
from youju.notify import run_cleanup
from youju.takedown import change_state

from .test_readers import seed

NOW = datetime(2026, 10, 23, 2, tzinfo=UTC)  # 北京时间 10/23 10:00
GONE = NOW - timedelta(days=2)  # 10/21 失效，10/28 清理
# D-124 ④：照 agy 合规审阅的改法，哪天停止展示（清理那天）写在这一句里
NOTICE = "原网页已无法访问，本平台将于10月28日停止展示；内容变动以官方最新发布为准"


@pytest.fixture
def env():
    """改环境变量（YOUJU_ 开头）再清掉配置缓存；用完恢复。"""
    before = dict(os.environ)

    def apply(**values):
        for key, value in values.items():
            os.environ[f"YOUJU_{key.upper()}"] = str(value)
        get_settings.cache_clear()

    yield apply
    os.environ.clear()
    os.environ.update(before)
    get_settings.cache_clear()


@pytest.fixture
def on(env):
    env(gone_cleanup_enabled="true", dev_openid="dev-user", agreement_version="2027-01")


@pytest.fixture
def client(session_factory, monkeypatch):
    def override():
        with session_factory() as session:
            yield session

    monkeypatch.setattr(v1, "now", lambda: NOW)
    download_api._times.clear()
    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()
    download_api._times.clear()


def gone_doc(session, n=1, *, gone_at=GONE, checked=NOW - timedelta(hours=3), **kw):
    doc, att, url, version = seed(session, n, **kw)
    doc.link_suspect_at = gone_at - timedelta(days=1)
    doc.link_gone_at = gone_at
    doc.link_checked_at = checked
    # 最近一次确认还打不开（判断不了的不算），到期清理看它
    doc.link_gone_seen_at = checked
    doc.last_seen_at = gone_at - timedelta(days=2)
    session.get(Source, doc.source_id).last_success_at = NOW - timedelta(hours=2)
    session.commit()
    return doc, att


def fresh(session, docs, when):
    """到期那天早上刚确认过还打不开，栏目也刚抓成功过。"""
    for doc in docs:
        doc.link_checked_at = doc.link_gone_seen_at = when - timedelta(hours=1)
        session.get(Source, doc.source_id).last_success_at = when - timedelta(hours=1)
    session.commit()


def agree(client, version="2027-01"):
    response = client.post("/api/v1/me/agreement", json={"version": version})
    assert response.status_code == 200, response.text


def save(client, doc):
    return client.post("/api/v1/me/saved", json={"docId": str(doc.id)})


def word_text(data: bytes) -> str:
    return "\n".join(p.text for p in WordDocument(BytesIO(data)).paragraphs)


# ---- 清理期的日期 ----


def test_window_dates_follow_beijing_days(env):
    env(gone_cleanup_enabled="true")
    # 北京时间 10/17 00:30 确认失效：10/17 算第 1 天，10/24 清理
    gone_at = datetime(2026, 10, 16, 16, 30, tzinfo=UTC)
    assert gone.gone_on(gone_at) == date(2026, 10, 17)
    assert gone.cleanup_on(gone_at) == date(2026, 10, 24)
    last = datetime(2026, 10, 23, 15, 59, tzinfo=UTC)  # 10/23 23:59
    assert gone.viewable(gone_at, last)
    assert not gone.viewable(gone_at, last + timedelta(minutes=1))  # 10/24 00:00 起不给
    assert not gone.viewable(None, last)
    env(gone_cleanup_enabled="false")
    assert not gone.viewable(gone_at, last) and gone.blocked(gone_at, last)
    env(gone_cleanup_enabled="true", agreement_version="2027-01")
    doc = Document(title="关于开展申报的通知", kind_override="申报通知", link_gone_at=gone_at)
    assert gone.payload(doc, True) == {
        "since": "2026-10-17",
        "cleanup": "2026-10-24",
        "notice": "原网页已无法访问，本平台将于10月24日停止展示；内容变动以官方最新发布为准",
        "cleanupNotice": "可以先保存或导出",
        "list": False,
        "window": True,
        "canSave": True,
        "canExport": True,
    }
    # 没有能读的正文和附件：不说"可以先保存或导出"，不给按钮
    empty = gone.payload(doc, True, readable=False)
    assert empty["cleanupNotice"] == ""
    assert empty["canSave"] is False and empty["canExport"] is False
    # 新版隐私指引还没上线（服务器协议版本早于 SAVED_AGREEMENT_VERSION）：只给导出
    env(agreement_version="2026-12")
    early = gone.payload(doc, True)
    assert early["cleanupNotice"] == "可以先导出"
    assert early["canSave"] is False and early["canExport"] is True
    roster = Document(title="拟认定名单公示", kind_override="名单", link_gone_at=gone_at)
    assert gone.payload(roster, False) == {
        "since": "2026-10-17",
        "cleanup": None,
        "notice": "原网页10月17日起已撤下",
        "cleanupNotice": "正文不再提供，名单请在“查名单”里按企业名称查询",
        "list": True,
        "window": False,
        "canSave": False,
        "canExport": False,
    }


# ---- 页面：清理期里照常给 ----


def test_window_gives_body_attachments_and_notice(client, session, on):
    doc, att = gone_doc(session)
    detail = client.get(f"/api/v1/documents/{doc.id}").json()
    assert detail["body"]["status"] == "ok" and detail["link_gone"] == "2026-10-21"
    assert detail["gone"] == {
        "since": "2026-10-21",
        "cleanup": "2026-10-28",
        "notice": NOTICE,
        "cleanupNotice": "可以先保存或导出",
        "list": False,
        "window": True,
        "canSave": True,
        "canExport": True,
    }
    [item] = detail["attachments"]
    assert (item["state"], item["readable"]) == ("ok", True)
    note = f"{NOTICE}。"
    body = client.get(f"/api/v1/documents/{doc.id}/reading").json()
    assert (body["status"], body["goneNote"]) == ("ok", note)
    page = client.get(f"/api/v1/documents/{doc.id}/attachments/{att.id}/reading").json()
    assert (page["status"], page["goneNote"], page["totalSegments"]) == ("ok", note, 45)
    [row] = client.get("/api/v1/search", params={"time": "不限"}).json()["items"]
    assert row["gone"]["cleanup"] == "2026-10-28" and row["link_gone"] == "2026-10-21"


def test_flag_off_keeps_d097(client, session, env):
    env(gone_cleanup_enabled="false")
    doc, att = gone_doc(session)
    detail = client.get(f"/api/v1/documents/{doc.id}").json()
    assert detail["body"] == {"status": "none", "reason": "link_gone"} and "gone" not in detail
    assert detail["attachments"][0]["state"] == "link_gone"
    body = client.get(f"/api/v1/documents/{doc.id}/reading").json()
    assert (body["status"], body["goneNote"]) == ("unavailable", "")
    [row] = client.get("/api/v1/search", params={"time": "不限"}).json()["items"]
    assert "gone" not in row
    assert client.post(f"/api/v1/documents/{doc.id}/gone-export").status_code == 404


def test_missing_documents_say_they_may_have_been_cleaned(client, session, env):
    """清理掉的通知从分享卡片、关注、旧列表点进来：开关开着时说可能已到期清理（agy 第 2 轮）；
    开关关着照旧。"""
    env(gone_cleanup_enabled="true")
    for path in ("/api/v1/documents/999999", "/api/v1/documents/999999/reading"):
        response = client.get(path)
        assert response.status_code == 404
        assert response.json()["detail"] == gone.NOT_FOUND
        assert response.json()["detail"].startswith(
            "没有找到这份文件，可能是原网页已无法访问、本平台已停止展示"
        )
    env(gone_cleanup_enabled="false")
    assert client.get("/api/v1/documents/999999").json()["detail"] == "没有找到这份文件"


def test_cleanup_day_and_later_go_back_to_d097(client, session, on):
    doc, _ = gone_doc(session, gone_at=NOW - timedelta(days=7))  # 10/16 失效，今天 10/23 清理
    detail = client.get(f"/api/v1/documents/{doc.id}").json()
    assert detail["body"] == {"status": "none", "reason": "link_gone"} and "gone" not in detail
    assert detail["attachments"][0]["state"] == "link_gone"


def _moved(session, doc):
    session.add(
        Document(
            source_id=doc.source_id,
            url="https://kjt.shandong.gov.cn/new.html",
            url_key="kjt.shandong.gov.cn/new.html",
            title=doc.title.replace("申报", " 申报"),  # 空白不同也算同一个标题
            publish_date=doc.publish_date,
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
    )


def _merged(session, doc):
    other, _, _, _ = seed(session, 2)
    session.add(DocumentMerge(duplicate_id=other.id, canonical_id=doc.id, match_key="k"))


def _listed(session, doc):
    doc.last_seen_at = NOW - timedelta(hours=1)


def _history(session, doc):
    for action in ("takedown", "restore"):
        session.add(
            DocumentTakedownEvent(
                document_id=doc.id,
                action=action,
                reason="其他",
                note="试",
                operated_by="测试",
                operated_at=NOW - timedelta(days=30),
            )
        )


def _changed(session, doc):
    old, _, _, _ = seed(session, 3)
    old.link_gone_at = None
    session.add(
        DocumentDeadline(
            document_id=old.id,
            fingerprint="c",
            kind="企业申报截止",
            candidate_date=date(2026, 10, 30),
            approved_date=date(2026, 10, 30),
            evidence="截止",
            confidence="高",
            status="changed",
            change_reason="延期",
            changed_to_document_id=doc.id,
        )
    )


KEEP = {
    "moved": _moved,
    "merged": _merged,
    "listed": _listed,
    "history": _history,
    "changed": _changed,
}


@pytest.mark.parametrize("reason", list(KEEP))
def test_kept_documents_are_not_cleaned_and_follow_d097(client, session, on, reason):
    """到期也不清理的（原栏目换了新地址、同一通知别的网址还能打开、原栏目还列着、有下架记录、
    被截止变更指着）：不写清理日期、不给保存，照 D-097 显示；到期不删。"""
    doc, _ = gone_doc(session)
    KEEP[reason](session, doc)
    session.commit()
    detail = client.get(f"/api/v1/documents/{doc.id}").json()
    assert "gone" not in detail and detail["body"]["status"] == "none"
    assert gone.keep_reasons(session, [doc], NOW) == {doc.id: reason}
    agree(client)
    assert save(client, doc).status_code == 409
    later = NOW + timedelta(days=8)
    fresh(session, [doc], later)
    if reason == "listed":
        doc.last_seen_at = later - timedelta(hours=1)  # 一直还列着
        session.commit()
    counts = gone.cleanup_expired(session, now=later)
    assert counts["due"] == 1 and counts[reason] == 1 and counts["deleted"] == 0
    assert session.get(Document, doc.id) is not None


def test_merge_group_is_cleaned_when_the_last_url_is_due(session, on):
    """归并组（同一份通知的不同网址）先后打不开：主条目到期时转载那份还在 7 天里，先不删；等最后一个
    也到期了整组一起删（agy 第 1 轮）。"""
    master, _ = gone_doc(session, 1, gone_at=NOW - timedelta(days=8))
    copy, _ = gone_doc(session, 2, gone_at=NOW - timedelta(days=3))
    session.add(DocumentMerge(duplicate_id=copy.id, canonical_id=master.id, match_key="k"))
    session.commit()
    assert gone.keep_reasons(session, [master], NOW, cleanup=True) == {master.id: "waiting"}
    counts = gone.cleanup_expired(session, now=NOW)
    assert counts["deleted"] == 0 and counts["waiting"] == 1
    later = NOW + timedelta(days=5)
    fresh(session, [master, copy], later)
    assert gone.cleanup_expired(session, now=later)["deleted"] == 2
    assert session.scalar(select(func.count()).select_from(Document)) == 0


def test_merge_group_waits_when_one_member_is_postponed(session, on):
    """组里两份都到期，转载那份这次推迟（网站当天判断不了，没确认）：整组都先不删（独立审阅第 2 轮）。"""
    master, _ = gone_doc(session, 1, gone_at=NOW - timedelta(days=9))
    copy, _ = gone_doc(session, 2, gone_at=NOW - timedelta(days=8), checked=NOW - timedelta(days=2))
    session.add(DocumentMerge(duplicate_id=copy.id, canonical_id=master.id, match_key="k"))
    session.commit()
    counts = gone.cleanup_expired(session, now=NOW)
    assert (counts["deleted"], counts["stale"], counts["waiting"]) == (0, 1, 1)
    assert session.scalar(select(func.count()).select_from(DocumentMerge)) == 1


def test_merge_group_all_in_window_still_shows_body(client, session, on):
    """组里几个网址都打不开、都还在 7 天里：主条目照常按清理期显示（显示时不看 waiting，agy 第 2 轮）。"""
    master, _ = gone_doc(session, 1, gone_at=NOW - timedelta(days=2))
    copy, _ = gone_doc(session, 2, gone_at=NOW - timedelta(days=1))
    session.add(DocumentMerge(duplicate_id=copy.id, canonical_id=master.id, match_key="k"))
    session.commit()
    assert gone.keep_reasons(session, [master], NOW) == {}
    detail = client.get(f"/api/v1/documents/{master.id}").json()
    assert detail["gone"]["window"] is True and detail["body"]["status"] == "ok"


def test_same_title_from_an_earlier_year_is_not_a_move(session, on):
    doc, _ = gone_doc(session)
    old = Document(
        source_id=doc.source_id,
        url="https://kjt.shandong.gov.cn/2025.html",
        url_key="kjt.shandong.gov.cn/2025.html",
        title=doc.title,
        publish_date=date(2025, 10, 20),
        first_seen_at=NOW - timedelta(days=360),
        last_seen_at=NOW - timedelta(days=300),
    )
    session.add(old)
    session.commit()
    assert gone.keep_reasons(session, [doc], NOW) == {}


def test_lists_are_kept_marked_and_readable_for_seven_days(client, session, on):
    """D-123 ④：名单类不按 7 天清理，照常留着，卡片和详情页标明"原网页某月某日起已撤下"；这 7 天里
    正文和别的通知一样照常给、能保存导出；7 天过后正文照现在不给（D-123 ⑥ 没定）。"""
    doc, _ = gone_doc(session, kind="名单")
    doc.title = "关于2026年度专精特新中小企业拟认定名单的公示"
    session.commit()
    detail = client.get(f"/api/v1/documents/{doc.id}").json()
    readable = detail["body"].get("status") == "ok" or any(
        item["readable"] for item in detail["attachments"]
    )
    assert detail["gone"] == {
        "since": "2026-10-21",
        "cleanup": None,
        "notice": "原网页10月21日起已撤下",
        "cleanupNotice": "",
        "list": True,
        "window": True,
        # 有没有东西可存：正文能读或有能读的附件
        "canSave": readable,
        "canExport": readable,
    }
    assert detail["body"] != {"status": "none", "reason": "link_gone"}
    reading = client.get(f"/api/v1/documents/{doc.id}/reading").json()
    assert reading["goneNote"] == "原网页10月21日起已撤下。"
    [row] = client.get("/api/v1/search", params={"time": "不限"}).json()["items"]
    assert row["gone"]["notice"] == "原网页10月21日起已撤下"
    # 7 天过后：照常留着、照常标明已撤下，正文照现在不给，不能保存
    doc.link_gone_at = NOW - timedelta(days=9)
    doc.link_checked_at = NOW - timedelta(hours=1)
    session.commit()
    later = client.get(f"/api/v1/documents/{doc.id}").json()
    assert later["gone"]["window"] is False and later["gone"]["list"] is True
    assert later["gone"]["cleanupNotice"] == "正文不再提供，名单请在“查名单”里按企业名称查询"
    assert later["body"] == {"status": "none", "reason": "link_gone"}
    agree(client)
    assert save(client, doc).status_code == 409
    # 名单类不清理：到期也不捞出来
    fresh(session, [doc], NOW)
    counts = gone.cleanup_expired(session, now=NOW)
    assert counts["due"] == 0 and counts["deleted"] == 0
    assert session.get(Document, doc.id) is not None


# ---- 本机：到期清理 ----


def test_cleanup_deletes_due_documents_only(session, on):
    due, _ = gone_doc(session, 1, gone_at=NOW - timedelta(days=7))
    stale, _ = gone_doc(
        session, 2, gone_at=NOW - timedelta(days=8), checked=NOW - timedelta(hours=40)
    )
    window, _ = gone_doc(session, 3)
    down, _ = gone_doc(session, 4, gone_at=NOW - timedelta(days=9))
    down.taken_down_at = NOW - timedelta(days=1)
    user = User(openid="u1", agreement_version="2027-01", agreement_at=NOW)
    session.add(user)
    session.flush()
    session.add_all(
        [
            Subscription(
                user_id=user.id,
                kind="deadline",
                template_id="t",
                document_id=due.id,
                status="unused",
            ),
            SavedDocument(
                user_id=user.id,
                document_id=due.id,
                title="t",
                issuer="i",
                site="s",
                region="r",
                kind="申报通知",
                gone_on=date(2026, 10, 16),
                content=json.dumps({"v": 1}),
                chars=1,
                saved_at=NOW - timedelta(days=2),
            ),
        ]
    )
    session.commit()
    assert gone.cleanup_expired(session, now=NOW, dry_run=True)["deleted"] == 1
    assert session.get(Document, due.id) is not None
    counts = gone.cleanup_expired(session, now=NOW)
    assert (counts["due"], counts["deleted"], counts["stale"]) == (2, 1, 1)
    session.expire_all()
    remaining = set(session.scalars(select(Document.id)))
    assert due.id not in remaining and {stale.id, window.id, down.id} <= remaining
    # 正文、截止提醒授权跟着删；用户保存的副本还在
    assert session.get(DocumentDetail, due.id) is None
    assert session.scalar(select(func.count()).select_from(Subscription)) == 0
    assert session.scalar(select(SavedDocument.document_id)) == due.id


def test_cleanup_does_nothing_when_off(session, env):
    env(gone_cleanup_enabled="false")
    doc, _ = gone_doc(session, gone_at=NOW - timedelta(days=9))
    assert gone.cleanup_expired(session, now=NOW)["deleted"] == 0
    assert session.get(Document, doc.id) is not None


def test_cleanup_skips_documents_restored_or_moved_meanwhile(session, on, monkeypatch):
    """挑出来以后、删之前恢复了（原网页又能打开）：锁住这一行再核对一遍，不删。"""
    doc, _ = gone_doc(session, gone_at=NOW - timedelta(days=7))
    real = gone._due

    def restored_before_delete(session_, now, ids=None, *, lock=False):
        if lock:
            session_.get(Document, doc.id).link_gone_at = None
            session_.flush()
        return real(session_, now, ids, lock=lock)

    monkeypatch.setattr(gone, "_due", restored_before_delete)
    assert gone.cleanup_expired(session, now=NOW)["deleted"] == 0
    assert session.get(Document, doc.id) is not None


def test_only_definite_gone_checks_count_for_cleanup(session, on):
    """判断不了的（网站出错、超时、robots）不算"确认还打不开"：网站维护期间不清理（D-123 ①：网站
    临时出故障不能误删；独立审阅第 1 轮）。"""
    doc, _ = gone_doc(session, gone_at=NOW - timedelta(days=7), checked=NOW - timedelta(days=2))
    assert link_check._apply(doc, "unknown", NOW - timedelta(hours=2)) == "unknown"
    assert doc.link_checked_at == NOW - timedelta(hours=2)
    assert doc.link_gone_seen_at == NOW - timedelta(days=2)
    session.commit()
    counts = gone.cleanup_expired(session, now=NOW)
    assert (counts["deleted"], counts["stale"]) == (0, 1)
    assert link_check._apply(doc, "404", NOW - timedelta(hours=1)) == "gone"
    assert doc.link_gone_seen_at == NOW - timedelta(hours=1)
    session.commit()
    assert gone.cleanup_expired(session, now=NOW)["deleted"] == 1
    # 能打开了：三个时间都清空
    other, _ = gone_doc(session, 2)
    assert link_check._apply(other, "ok", NOW) == "ok"
    assert other.link_gone_at is None and other.link_gone_seen_at is None


def test_cleanup_waits_when_the_column_was_not_crawled(session, on):
    """原栏目 48 小时没抓成功：不知道还列不列着，先不删（独立审阅第 1 轮）。"""
    doc, _ = gone_doc(session, gone_at=NOW - timedelta(days=7))
    session.get(Source, doc.source_id).last_success_at = NOW - timedelta(days=3)
    session.commit()
    counts = gone.cleanup_expired(session, now=NOW)
    assert (counts["deleted"], counts["source_stale"]) == (0, 1)


def test_one_failed_delete_does_not_stop_the_batch(session, on, monkeypatch):
    first, _ = gone_doc(session, 1, gone_at=NOW - timedelta(days=8))
    second, _ = gone_doc(session, 2, gone_at=NOW - timedelta(days=7))
    real = gone._due

    def flaky(session_, now, ids=None, *, lock=False):
        if lock and ids == [first.id]:
            raise RuntimeError("锁等待超时")
        return real(session_, now, ids, lock=lock)

    monkeypatch.setattr(gone, "_due", flaky)
    counts = gone.cleanup_expired(session, now=NOW)
    assert (counts["deleted"], counts["failed"]) == (1, 1)
    remaining = set(session.scalars(select(Document.id)))
    assert first.id in remaining and second.id not in remaining


def test_saving_waits_for_the_new_privacy_version(client, session, env):
    """开关开着、服务器协议还是旧版（早于 SAVED_AGREEMENT_VERSION）：不给保存（接口 404，按钮不出），
    导出照常；我的页不放入口（没保存过时）。"""
    env(gone_cleanup_enabled="true", dev_openid="dev-user", agreement_version="2026-12")
    doc, _ = gone_doc(session)
    gone_info = client.get(f"/api/v1/documents/{doc.id}").json()["gone"]
    assert (gone_info["canSave"], gone_info["canExport"]) == (False, True)
    agree(client, "2026-12")
    assert save(client, doc).status_code == 404
    assert client.get("/api/v1/me").json()["saved"] is None
    assert client.post(f"/api/v1/documents/{doc.id}/gone-export").status_code == 200


def test_cli_saved_purge_removes_copies_of_a_cleaned_notice(client, session, on, capsys):
    """通知已经清理、管理台下架不了，收到投诉时在服务器上按编号删副本（D-123 ④）。"""
    from youju import cli

    doc, _ = gone_doc(session)
    agree(client)
    assert save(client, doc).status_code == 201
    session.execute(Document.__table__.delete().where(Document.id == doc.id))
    session.commit()
    assert cli.main(["saved-purge", "--doc", str(doc.id)]) == 0
    assert "删了 1 份用户保存的副本" in capsys.readouterr().out
    assert session.scalar(select(func.count()).select_from(SavedDocument)) == 0


def test_link_check_rechecks_window_documents_daily_first(session, env):
    """清理期里的每天再查一次（D-123 ①），不管发布了多久，排在最前面。"""
    doc, _ = gone_doc(session, checked=NOW - timedelta(hours=21))
    doc.publish_date = date(2026, 3, 1)  # 发布 200 多天：平时一周查一次
    session.commit()
    env(gone_cleanup_enabled="false")
    picked, _ = link_check._select(session, NOW, 10)
    assert doc.id not in [item[0] for item in picked]
    env(gone_cleanup_enabled="true")
    fresh, _, _, _ = seed(session, 2)  # 近 30 天、没查过的
    picked, left = link_check._select(session, NOW, 10)
    assert [item[0] for item in picked] == [doc.id, fresh.id] and left == 0


def test_link_check_checks_old_notices_and_policies_monthly(session, env):
    """D-124 ①：发布超过 90 天的申报通知、政策也查，每月一次，排在最后（没查过的先查）；别的类型照旧
    只查 90 天以内的；开关关着照旧。"""
    old = date(2026, 3, 1)  # 发布 200 多天
    apply, _, _, _ = seed(session, 1)  # 申报通知，没查过
    policy, _, _, _ = seed(session, 2, kind="政策")
    guide, _, _, _ = seed(session, 3, kind="资讯与指南")
    recent_check, _, _, _ = seed(session, 4, kind="政策")
    for doc in (apply, policy, guide, recent_check):
        doc.publish_date = old
    policy.link_checked_at = NOW - timedelta(days=31)
    recent_check.link_checked_at = NOW - timedelta(days=10)  # 这个月查过了
    session.commit()
    env(gone_cleanup_enabled="false")
    assert link_check._select(session, NOW, 10) == ([], 0)
    env(gone_cleanup_enabled="true")
    live, _, _, _ = seed(session, 5)  # 近 30 天、没查过的排在前面
    picked, left = link_check._select(session, NOW, 10)
    assert [item[0] for item in picked] == [live.id, apply.id, policy.id] and left == 0
    # 查到像是撤下了：上面几队接着管（隔天确认），这一队不再重复挑
    apply.link_checked_at = apply.link_suspect_at = NOW - timedelta(hours=21)
    session.commit()
    picked, _ = link_check._select(session, NOW, 10)
    assert [item[0] for item in picked].count(apply.id) == 1


def test_dry_run_works_with_flag_off_and_lists_documents(session, env, capsys):
    """D-124 ⑤：打开开关那天会一次清掉积下来的，要先试跑报数：只数不删的试跑开关关着也能跑，可以逐份
    列出；真删要开关开着。"""
    from youju import cli

    env(gone_cleanup_enabled="false")
    doc, _ = gone_doc(session, gone_at=NOW - timedelta(days=9))
    stale, _ = gone_doc(session, 2, gone_at=NOW - timedelta(days=12))
    stale.link_gone_seen_at = NOW - timedelta(days=3)  # 最近没确认过还打不开：先推迟
    session.commit()
    rows: list[dict] = []
    counts = gone.cleanup_expired(session, now=NOW, dry_run=True, listing=rows)
    assert (counts["due"], counts["deleted"], counts["stale"]) == (2, 1, 1)
    assert rows == [
        {
            "id": stale.id,
            "since": "2026-10-11",
            "state": "推迟：最近 30 小时没确认还打不开",
            "title": stale.title,
        },
        {"id": doc.id, "since": "2026-10-14", "state": "会删", "title": doc.title},
    ]
    assert gone.cleanup_expired(session, now=NOW)["deleted"] == 0  # 开关关着不真删
    assert session.scalar(select(func.count()).select_from(Document)) == 2
    assert cli.main(["gone-cleanup"]) == 1
    assert "只数不删可以加 --dry-run" in capsys.readouterr().out
    assert cli.main(["gone-cleanup", "--list"]) == 1
    capsys.readouterr()
    assert cli.main(["gone-cleanup", "--dry-run", "--list"]) == 0
    out = capsys.readouterr().out
    assert "合计到期该删 2 份" in out and f"#{doc.id}\t" in out and f"#{stale.id}\t" in out
    assert session.scalar(select(func.count()).select_from(Document)) == 2


def test_cleaned_ids_only_with_flag_on(session, env):
    """D-124 ②：库里没有了的编号当是到期清理掉的（只有清理会删通知）；开关关着不判断。"""
    doc, _ = gone_doc(session)
    env(gone_cleanup_enabled="true")
    assert gone.cleaned_ids(session, [doc.id, 999999, None, "", "999998", 0, "x"]) == {
        999999,
        999998,
    }
    env(gone_cleanup_enabled="false")
    assert gone.cleaned_ids(session, [999999]) == set()


def test_cli_check_links_runs_cleanup_after_checking(session, on, monkeypatch, capsys):
    from youju import cli

    doc, _ = gone_doc(session, gone_at=NOW - timedelta(days=7))
    calls = []
    monkeypatch.setattr(
        "youju.link_check.check_links",
        lambda s, **kw: (
            calls.append("check")
            or dict.fromkeys(
                ("checked", "ok", "suspect", "gone", "unknown", "withdrawn_page", "left"), 0
            )
            | {"stale_suspect": 0}
        ),
    )
    real = gone.cleanup_expired
    monkeypatch.setattr(
        gone, "cleanup_expired", lambda s, **kw: calls.append("cleanup") or real(s, now=NOW)
    )
    assert cli.main(["check-links"]) == 0
    assert calls == ["check", "cleanup"]
    assert "原网页失效到期清理：到期 1 份（名单类不算），删了 1 份" in capsys.readouterr().out
    assert session.scalar(select(Document.id).where(Document.id == doc.id)) is None


# ---- 我的保存 ----


def test_save_list_read_export_delete(client, session, on):
    doc, _ = gone_doc(session)
    agree(client)
    response = save(client, doc)
    assert response.status_code == 201, response.text
    item = response.json()["item"]
    assert item["docId"] == str(doc.id) and item["goneOn"] == "2026-10-21"
    assert item["cleaned"] is False and item["chars"] > 0 and item["truncated"] is False
    # 副本写打不开的那天起（D-124 ④的说法；不说"将于某日停止展示"，保存以后可能已经停止展示）
    assert (
        item["note"]
        == "原网页10月21日起无法访问；内容变动以官方最新发布为准。这是你10月23日保存的副本。"
    )
    [listed] = client.get("/api/v1/me/saved").json()["items"]
    assert listed == item
    base = f"/api/v1/me/saved/{item['id']}/reading"
    body = client.get(base).json()
    assert body["status"] == "ok" and body["segments"][0]["text"].startswith("申报说明。")
    assert body["goneNote"] == item["note"] and body["toc"]["attachments"][0]["readable"]
    att = client.get(base, params={"part": "1"}).json()
    assert att["totalSegments"] == 45 and att["segments"][0]["text"] == "第1段申报说明"
    exported = client.post(f"/api/v1/me/saved/{item['id']}/export")
    assert exported.status_code == 200 and exported.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml"
    )
    text = word_text(exported.content)
    assert doc.title in text and item["note"] in text and "第45段申报说明" in text
    # 通知到期清理以后副本照常在
    session.delete(session.get(DocumentDetail, doc.id))
    session.execute(Document.__table__.delete().where(Document.id == doc.id))
    session.commit()
    [listed] = client.get("/api/v1/me/saved").json()["items"]
    assert listed["cleaned"] is True
    assert client.get(base).json()["status"] == "ok"
    assert client.delete(f"/api/v1/me/saved/{item['id']}").status_code == 204
    assert client.get("/api/v1/me/saved").json()["items"] == []


def test_save_again_replaces_the_copy(client, session, on):
    doc, _ = gone_doc(session)
    agree(client)
    first = save(client, doc).json()["item"]
    session.get(DocumentDetail, doc.id).body_text = "改过的正文。" * 50
    session.commit()
    second = save(client, doc).json()["item"]
    assert second["id"] == first["id"]
    content = json.loads(session.scalar(select(SavedDocument.content)))
    assert content["body"]["parts"][0].startswith("改过的正文。")


def test_save_refusals(client, session, env):
    env(
        gone_cleanup_enabled="true",
        dev_openid="dev-user",
        agreement_version="2027-01",
        agreement_min_version="2026-12",
        saved_max_per_user="1",
    )
    doc, _ = gone_doc(session, 1)
    other, _ = gone_doc(session, 2)
    assert save(client, doc).status_code == 403  # 还没同意
    agree(client, "2026-12")
    refused = save(client, doc)
    assert refused.status_code == 403 and "新版隐私保护指引" in refused.json()["detail"]
    agree(client)
    live, _, _, _ = seed(session, 3)
    assert save(client, live).status_code == 409  # 原网页能打开
    old, _ = gone_doc(session, 4, gone_at=NOW - timedelta(days=7))
    assert save(client, old).status_code == 409  # 已到清理日期
    assert save(client, doc).status_code == 201
    full = save(client, other)
    assert full.status_code == 409 and "最多保存 1 份" in full.json()["detail"]
    assert save(client, doc).status_code == 201  # 已保存的那份可以再存
    empty, _ = gone_doc(session, 5, body="")
    env(saved_max_per_user="10")
    session.query(SavedDocument).delete()
    session.commit()
    # 没有正文、附件也读不了：没东西可存
    from youju.models import AttachmentTextUrl

    session.query(AttachmentTextUrl).delete()
    session.commit()
    assert save(client, empty).status_code == 409
    env(gone_cleanup_enabled="false")
    assert save(client, doc).status_code == 404


def test_takedown_removes_saved_copies(client, session, on, monkeypatch):
    """D-123 ④：下架的通知，用户保存的副本一起删。单库时下架当场删；2.0 里下架经同步过来（行和
    下架记录直接写进库），读的时候先不给，每天的 cleanup 删掉；下架后又恢复的也删；恢复以后
    再保存的不删。"""
    a, _ = gone_doc(session, 1)
    b, _ = gone_doc(session, 2)
    agree(client)
    assert save(client, a).status_code == 201 and save(client, b).status_code == 201
    change_state(session, a.id, action="takedown", note="投诉", operator="测试", reason="侵权投诉")
    assert [i["docId"] for i in client.get("/api/v1/me/saved").json()["items"]] == [str(b.id)]
    assert session.scalar(select(func.count()).select_from(SavedDocument)) == 1
    # 2.0：同步过来的下架、恢复（下架记录在保存之后）
    for action, at in (
        ("takedown", NOW + timedelta(minutes=1)),
        ("restore", NOW + timedelta(minutes=2)),
    ):
        session.add(
            DocumentTakedownEvent(
                document_id=b.id,
                action=action,
                reason="侵权投诉",
                note="",
                operated_by="x",
                operated_at=at,
            )
        )
    session.commit()
    assert client.get("/api/v1/me/saved").json()["items"] == []
    assert run_cleanup(session)["saved_taken_down"] == 1
    assert session.scalar(select(func.count()).select_from(SavedDocument)) == 0
    # 有过下架、恢复的通知不清理（下架记录要留着），也就不在清理期里，不能再保存
    monkeypatch.setattr(v1, "now", lambda: NOW + timedelta(minutes=3))
    assert save(client, b).status_code == 409
    # 下架记录早于保存时间的副本不删（读的条件和删的条件一样只看保存以后的下架）
    user_id = session.scalar(select(User.id))
    session.add(
        SavedDocument(
            user_id=user_id,
            document_id=b.id,
            title="t",
            issuer="i",
            site="s",
            region="r",
            kind="申报通知",
            gone_on=date(2026, 10, 21),
            content=json.dumps({"v": 1}),
            chars=1,
            saved_at=NOW + timedelta(minutes=3),
        )
    )
    session.commit()
    assert run_cleanup(session)["saved_taken_down"] == 0
    assert len(client.get("/api/v1/me/saved").json()["items"]) == 1


def test_profile_my_data_and_account_deletion(client, session, on):
    doc, _ = gone_doc(session)
    assert client.get("/api/v1/me").json()["saved"] == {"enabled": True, "count": 0, "max": 200}
    agree(client)
    item = save(client, doc).json()["item"]
    assert client.get("/api/v1/me").json()["saved"]["count"] == 1
    data = client.get("/api/v1/me/data").json()
    assert data["savedTotal"] == 1 and data["saved"][0]["title"] == item["title"]
    assert "content" not in data["saved"][0]
    assert client.delete("/api/v1/me").status_code == 204
    assert session.scalar(select(func.count()).select_from(SavedDocument)) == 0


def test_saved_copies_stay_reachable_when_the_flag_is_turned_off(client, session, env):
    env(gone_cleanup_enabled="true", dev_openid="dev-user", agreement_version="2027-01")
    doc, _ = gone_doc(session)
    agree(client)
    item = save(client, doc).json()["item"]
    env(gone_cleanup_enabled="false")
    assert client.get("/api/v1/me").json()["saved"] == {"enabled": False, "count": 1, "max": 200}
    assert len(client.get("/api/v1/me/saved").json()["items"]) == 1
    assert client.get(f"/api/v1/me/saved/{item['id']}/reading").status_code == 200
    assert client.post(f"/api/v1/me/saved/{item['id']}/export").status_code == 200
    assert client.delete(f"/api/v1/me/saved/{item['id']}").status_code == 204
    assert client.get("/api/v1/me").json()["saved"] is None


def test_other_users_copies_are_invisible(client, session, on):
    doc, _ = gone_doc(session)
    agree(client)
    item = save(client, doc).json()["item"]
    other = User(openid="someone-else", agreement_version="2027-01", agreement_at=NOW)
    session.add(other)
    session.commit()
    os.environ["YOUJU_DEV_OPENID"] = "someone-else"
    get_settings.cache_clear()
    assert client.get("/api/v1/me/saved").json()["items"] == []
    assert client.get(f"/api/v1/me/saved/{item['id']}/reading").status_code == 404
    assert client.post(f"/api/v1/me/saved/{item['id']}/export").status_code == 404
    assert client.delete(f"/api/v1/me/saved/{item['id']}").status_code == 204
    assert session.scalar(select(func.count()).select_from(SavedDocument)) == 1


def test_gone_export_needs_no_login(client, session, on):
    doc, _ = gone_doc(session)
    response = client.post(f"/api/v1/documents/{doc.id}/gone-export")
    assert response.status_code == 200
    text = word_text(response.content)
    assert f"{NOTICE}。" in text and "第1段申报说明" in text
    assert "清理" not in text and "撤回或更正" not in text
    assert "来源：山东省科技厅网站" in text and "http" not in text
    # 原网页已经打不开、到期就清理：不写"在小程序里可以查看原文和更新"（agy 合规审阅 11/8）
    assert "查看原文和更新" not in text
    live, _, _, _ = seed(session, 2)
    assert client.post(f"/api/v1/documents/{live.id}/gone-export").status_code == 409


def test_restricted_api_account_can_save_and_delete(
    session, on, restricted_api_factory, monkeypatch
):
    """按部署文件给 api 账号的授权（mysql_accounts.sql"我的保存"一段）：保存、再存、删除、注销。"""
    from youju import access_log

    doc, _ = gone_doc(session)
    monkeypatch.setattr(v1, "now", lambda: NOW)
    with restricted_api_factory() as limited:
        monkeypatch.setattr(access_log, "get_engine", lambda: limited.kw["bind"])

        def override():
            with limited() as s:
                yield s

        app.dependency_overrides[get_session] = override
        try:
            with TestClient(app) as c:
                agree(c)
                item = save(c, doc).json()["item"]
                assert save(c, doc).status_code == 201
                assert c.delete(f"/api/v1/me/saved/{item['id']}").status_code == 204
                assert save(c, doc).status_code == 201
                assert c.delete("/api/v1/me").status_code == 204
        finally:
            app.dependency_overrides.clear()
    assert session.scalar(select(func.count()).select_from(SavedDocument)) == 0


def test_saved_table_is_user_data_not_public():
    from youju.public_snapshot import EXCLUDED_TABLES, PUBLIC_TABLES

    assert "saved_documents" in EXCLUDED_TABLES and "saved_documents" not in PUBLIC_TABLES
    # 不对 documents 建外键：通知清理掉以后副本还在
    table = SavedDocument.__table__
    assert {fk.column.table.name for fk in table.foreign_keys} == {"users"}
    assert DeadlineClue.__table__ is not None and Source.__table__ is not None


def test_build_cuts_very_long_copies(session, on, monkeypatch):
    doc, _ = gone_doc(session, body="很长的正文。" * 3000)
    monkeypatch.setattr(v1, "now", lambda: NOW)
    monkeypatch.setattr(saved, "MAX_BYTES", 50_000)
    content, chars, truncated = saved.build(session, doc, NOW)
    assert truncated and content["body"]["parts"] and chars < 20_000
    assert content["attachments"][0]["note"] == "附件太长，没能保存"
