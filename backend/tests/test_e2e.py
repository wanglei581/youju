"""从离线来源抓取到小程序接口的最小回归路径。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.conftest import read_fixture_html
from youju.api import me, v1
from youju.api.app import app
from youju.classify import doc_kind
from youju.crawler import service as crawler_service
from youju.crawler.detail_service import fetch_details
from youju.crawler.fetcher import FetchResult
from youju.crawler.service import crawl_sources
from youju.db import get_session
from youju.extract.service import extract_existing
from youju.lists.service import parse_lists
from youju.models import Document, Event, Feedback, Follow, ListEntry, Source, Subscription, User
from youju.notify import run_notify
from youju.sources.importer import import_sources

NOW = datetime(2026, 9, 27, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
NOW_UTC = NOW.astimezone(UTC)
ADMIN_HEADERS = {"X-Admin-Token": "e2e-admin-token"}


class FixtureFetcher:
    """按请求顺序返回内存中的 fixture，不产生网络请求。"""

    def __init__(self, responses: dict[str, FetchResult | list[FetchResult]]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        response = self.responses[url]
        if isinstance(response, list):
            return response.pop(0)
        return response


def _html_response(url: str, text: str) -> FetchResult:
    return FetchResult(
        url,
        "ok",
        final_url=url,
        http_status=200,
        content_type="text/html; charset=utf-8",
        text=text,
    )


def _source_csv(path: Path, url: str) -> Path:
    path.write_text(
        "层级,省份,部门,部门全称,栏目名称,栏目URL,HTTP状态,页面日期数,最新日期样例,"
        "验证状态,验证日期,备注\n"
        f"省级,北京,科技,北京市科学技术委员会,通知公告,{url},200,20,2026-09-23,"
        "已验证（列表页，含近期日期）,2026-09-27,离线回归夹具\n",
        encoding="utf-8-sig",
    )
    return path


def _new_listing_page(base: str) -> str:
    marker = '<ul class="list">'
    start = base.index(marker)
    end = base.index("</ul>", start)
    row = (
        '<li><a href="/e2e/extension.html" title="关于延长2026年度高新技术企业申报受理截止时间的通知">'
        "关于延长2026年度高新技术企业申报受理截止时间的通知"
        '</a><span class="fr">2026-09-25</span></li>'
    )
    return base[:end] + row + base[end:]


def _list_detail_page() -> str:
    return """
    <html><body><div id="zoom">
      <p>关于北京市2026年第三批拟入库科技型中小企业名单的公示。</p>
      <table>
        <tr><th>企业名称</th><th>统一社会信用代码</th></tr>
        <tr><td>北京星河科技有限公司</td><td>91310117588657256C</td></tr>
      </table>
      <a href="/fixtures/heilongjiang_30.xlsx">附件：名单.xlsx</a>
    </div></body></html>
    """


def _test_settings() -> SimpleNamespace:
    return SimpleNamespace(
        trust_wx_headers=False,
        dev_openid="e2e-user",
        template_newdocs={"id": "tpl-newdocs", "fields": {"thing1": "summary", "number2": "count"}},
        template_deadline=None,
        agreement_version="2026-09",
        send_wx_messages=False,
        retain_events_days=90,
        retain_feedback_days=180,
        retain_used_subscriptions_days=30,
        retain_unused_subscriptions_days=365,
    )


def test_offline_ingest_to_miniapp_e2e(session_factory, session, tmp_path, monkeypatch) -> None:
    """真实业务顺序：来源→抓取→详情→核对→名单→关注提醒→纠错注销。"""

    monkeypatch.setattr(v1, "now", lambda: NOW)
    monkeypatch.setattr(me, "get_settings", _test_settings)
    monkeypatch.setattr(
        "youju.api.app.get_settings", lambda: SimpleNamespace(admin_token="e2e-admin-token")
    )

    def override_session():
        with session_factory() as request_session:
            yield request_session

    app.dependency_overrides[get_session] = override_session
    try:
        source_url = "https://kw.beijing.gov.cn/zwgk/tzgg/"
        source_csv = _source_csv(tmp_path / "sources.csv", source_url)
        imported = import_sources(session, source_csv)
        assert (imported.created, imported.enabled, imported.disabled) == (1, 1, 0)
        source = session.scalar(select(Source).where(Source.url == source_url))
        assert source is not None and source.enabled

        listing_fixture = read_fixture_html("beijing_kw_tzgg")
        listing_fetcher = FixtureFetcher(
            {
                source_url: [
                    _html_response(source_url, listing_fixture),
                    _html_response(source_url, _new_listing_page(listing_fixture)),
                ]
            }
        )
        # 固定抓取时间，使 first_seen_at 与用户关注建立时间的先后可断言。
        monkeypatch.setattr(crawler_service, "_now", lambda: NOW_UTC - datetime.resolution)
        first = crawl_sources(session_factory, [source.id], listing_fetcher, max_workers=1)[0]
        assert (first.status, first.baseline, first.items_found, first.items_new) == (
            "ok",
            True,
            20,
            20,
        )
        assert session.scalar(select(func.count(Document.id))) == 20
        assert (
            session.scalar(select(func.count(Document.id)).where(Document.is_baseline.is_(False)))
            == 0
        )

        client = TestClient(app)
        assert (
            client.post(
                "/api/v1/me/follows",
                json={"name": "未同意时不应保存", "conditions": {"time": "不限"}},
            ).status_code
            == 403
        )
        assert client.post("/api/v1/me/agreement", json={"version": "2026-09"}).status_code == 200
        follow_response = client.post(
            "/api/v1/me/follows",
            json={
                "name": "高企延期通知",
                "conditions": {"q": "延长", "time": "不限"},
            },
        )
        assert follow_response.status_code == 201
        follow_id = follow_response.json()["id"]

        monkeypatch.setattr(crawler_service, "_now", lambda: NOW_UTC + datetime.resolution)
        second = crawl_sources(session_factory, [source.id], listing_fetcher, max_workers=1)[0]
        assert (second.status, second.baseline, second.items_found, second.items_new) == (
            "ok",
            False,
            21,
            1,
        )
        session.expire_all()
        new_doc = session.scalar(
            select(Document).where(
                Document.title == "关于延长2026年度高新技术企业申报受理截止时间的通知"
            )
        )
        assert new_doc is not None and not new_doc.is_baseline

        original = session.scalar(
            select(Document).where(Document.title.contains("国家量子科技关键材料器件设备研发"))
        )
        list_doc = session.scalar(
            select(Document).where(Document.title.contains("第三批拟入库科技型中小企业名单"))
        )
        assert original is not None and list_doc is not None
        assert client.get(f"/api/v1/me/follows/{follow_id}/new").json()["total"] == 1

        original_detail = read_fixture_html("detail_liaoning_344")
        new_detail = read_fixture_html("detail_fujian_915")
        list_detail = _list_detail_page()
        attachment_url = "https://kw.beijing.gov.cn/fixtures/heilongjiang_30.xlsx"
        details_fetcher = FixtureFetcher(
            {
                original.url: _html_response(original.url, original_detail),
                new_doc.url: _html_response(new_doc.url, new_detail),
                list_doc.url: [
                    _html_response(list_doc.url, list_detail),
                    _html_response(list_doc.url, list_detail),
                ],
                attachment_url: FetchResult(
                    attachment_url,
                    "ok",
                    final_url=attachment_url,
                    http_status=200,
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    content=(
                        Path(__file__).parent / "fixtures" / "lists" / "heilongjiang_30.xlsx"
                    ).read_bytes(),
                ),
            }
        )
        # 北京 fixture 还包含一份与本场景无关的名单通知；让它走离线空详情，
        # 以验证名单任务会跳过没有可解析企业表格的文件。
        other_list_docs = session.scalars(
            select(Document).where(
                Document.title.contains("入围全国赛企业名单"),
                Document.id != list_doc.id,
            )
        ).all()
        for other in other_list_docs:
            details_fetcher.responses[other.url] = _html_response(
                other.url, "<html><body><div id='zoom'>没有名单表格。</div></body></html>"
            )
        detail_results = fetch_details(
            session,
            limit=3,
            fetcher=details_fetcher,
            document_ids=[original.id, list_doc.id, new_doc.id],
        )
        assert [result.status for result in detail_results] == ["ok", "ok", "ok"]

        list_report = parse_lists(session, since=date(2026, 9, 1), fetcher=details_fetcher)
        assert list_report.parsed_documents == 1
        assert list_report.attachments_fetched == 1
        assert (
            session.scalar(
                select(func.count(ListEntry.id)).where(ListEntry.document_id == list_doc.id)
            )
            == 31
        )

        docs, added = extract_existing(session, document_ids=[original.id, new_doc.id])
        assert (docs, added) == (2, 3)
        assert client.get(f"/api/v1/documents/{original.id}").json()["deadline"] is None
        search_original = client.get(
            "/api/v1/search", params={"q": "国家量子科技关键材料器件设备研发", "time": "不限"}
        ).json()
        assert search_original["total"] == 1
        assert search_original["items"][0]["id"] == str(original.id)
        assert search_original["items"][0]["url"] == original.url
        pending = client.get("/api/admin/deadlines", headers=ADMIN_HEADERS).json()
        original_pending = [row for row in pending if row["document_id"] == original.id]
        new_pending = [row for row in pending if row["document_id"] == new_doc.id]
        assert len(original_pending) == 2 and len(new_pending) == 1
        original_main = next(row for row in original_pending if row["kind"] == "企业申报截止")
        assert client.post(
            f"/api/admin/deadlines/{original_main['id']}/approve",
            json={"reviewed_by": "e2e-reviewer"},
            headers=ADMIN_HEADERS,
        ).json() == {"status": "approved"}

        approved_detail = client.get(f"/api/v1/documents/{original.id}").json()
        assert approved_detail["deadline"]["date"] == "2026-10-18"
        assert approved_detail["deadline"]["verified"] is True
        calendar = client.get("/api/v1/calendar").json()
        assert any(
            item["docId"] == str(original.id) and item["due"] == "2026-10-18"
            for item in calendar["items"]
        )
        assert all(item["verified"] for item in calendar["items"])
        assert not any(
            item["docId"] == str(original.id) and item["due"] == "2026-10-20"
            for item in calendar["items"]
        )

        new_main = new_pending[0]
        assert (
            client.post(
                f"/api/admin/deadlines/{new_main['id']}/approve",
                json={"reviewed_by": "e2e-reviewer"},
                headers=ADMIN_HEADERS,
            ).status_code
            == 200
        )
        assert (
            client.get(f"/api/v1/documents/{new_doc.id}").json()["deadline"]["date"] == "2026-10-15"
        )
        name_lookup = client.get("/api/v1/lists/lookup", params={"q": "北京星河科技有限公司"})
        code_lookup = client.get("/api/v1/lists/lookup", params={"q": "91310117588657256C"})
        assert name_lookup.status_code == code_lookup.status_code == 200
        assert name_lookup.json()["items"][0]["matched"]["via"] == "name"
        assert code_lookup.json()["items"][0]["matched"]["via"] == "creditCode"
        assert name_lookup.json()["items"][0]["title"] == list_doc.title
        assert client.get("/api/v1/lists/lookup", params={"q": "张三"}).status_code == 422

        change = client.post(
            f"/api/admin/deadlines/{original_main['id']}/change",
            json={
                "reviewed_by": "e2e-reviewer",
                "reason": "延期",
                "new_document_id": new_doc.id,
            },
            headers=ADMIN_HEADERS,
        )
        assert change.status_code == 200
        changed_detail = client.get(f"/api/v1/documents/{original.id}").json()
        assert changed_detail["deadline"] is None
        assert changed_detail["deadline_changes"] == [
            {
                "label": "企业申报截止",
                "date": "2026-10-18",
                "time": "",
                "reason": "延期",
                "newDocId": str(new_doc.id),
                "newTitle": new_doc.title,
            }
        ]
        calendar_after_change = client.get("/api/v1/calendar").json()["items"]
        assert not any(
            item["docId"] == str(original.id) and item["due"] == "2026-10-18"
            for item in calendar_after_change
        )
        assert any(
            item["docId"] == str(new_doc.id) and item["due"] == "2026-10-15"
            for item in calendar_after_change
        )

        subscription = client.post(
            "/api/v1/me/subscriptions",
            json={"kind": "newDocs", "templateId": "tpl-newdocs", "followId": follow_id},
        )
        assert subscription.status_code == 201
        subscription_id = subscription.json()["id"]
        feedback = client.post(
            "/api/v1/feedback",
            json={"docId": original.id, "content": "截止日期有误，联系电话 13812345678"},
        )
        assert feedback.status_code == 201
        feedback_id = feedback.json()["id"]
        event = client.post("/api/v1/events", json={"name": "search", "props": {"count": 1}})
        assert event.status_code == 202

        admin_feedback = client.get("/api/admin/feedback", headers=ADMIN_HEADERS)
        assert admin_feedback.status_code == 200
        row = next(item for item in admin_feedback.json() if item["id"] == feedback_id)
        assert "13812345678" not in row["content"] and "［已隐去］" in row["content"]
        assert client.post(
            f"/api/admin/feedback/{feedback_id}/handle", headers=ADMIN_HEADERS
        ).json() == {"handled": True, "changed": True}

        notify_settings = SimpleNamespace(
            send_wx_messages=True,
            template_newdocs={
                "id": "tpl-newdocs",
                "fields": {"thing1": "summary", "number2": "count"},
            },
            template_deadline=None,
            agreement_version="2026-09",
        )
        sent_payloads: list[dict] = []

        def fake_sender(payload: dict) -> dict:
            sent_payloads.append(payload)
            return {"errcode": 0}

        first_notify = run_notify(session, settings=notify_settings, sender=fake_sender, stamp=NOW)
        second_notify = run_notify(session, settings=notify_settings, sender=fake_sender, stamp=NOW)
        assert first_notify["sent"] == 1 and second_notify["sent"] == 0
        assert len(sent_payloads) == 1
        assert session.get(Subscription, subscription_id).status == "sent"
        assert sent_payloads[0]["page"] == "pages/follow/index"

        home = client.get("/api/v1/home").json()
        search_all = client.get("/api/v1/search", params={"time": "不限"}).json()
        coverage = client.get("/api/v1/coverage").json()
        total_documents = session.scalar(select(func.count(Document.id)))
        week_start = NOW.date() - timedelta(days=NOW.weekday())
        week_end = week_start + timedelta(days=7)
        week_docs = session.scalars(
            select(Document).where(
                Document.publish_date >= week_start,
                Document.publish_date < week_end,
            )
        ).all()
        week_kinds = [doc_kind(doc.title) for doc in week_docs]
        assert home["stats"]["week_total"] == len(week_docs)
        assert home["stats"]["apply"] == week_kinds.count("申报通知")
        assert home["stats"]["lists"] == week_kinds.count("名单")
        assert (
            home["stats"]["other"]
            == len(week_kinds) - home["stats"]["apply"] - home["stats"]["lists"]
        )
        assert search_all["total"] == search_all["total_all"] == total_documents
        assert home["stats"]["sources"] == coverage["sources"] == 1
        assert coverage["provinces"] == 1 and coverage["regions"][0]["name"] == "北京"
        assert home["stats"]["followed_new"] is None

        renamed = client.put(
            f"/api/v1/me/follows/{follow_id}", json={"name": "高企延期通知（已改名）"}
        )
        assert renamed.status_code == 200 and renamed.json()["name"] == "高企延期通知（已改名）"
        personal_data = client.get("/api/v1/me/data").json()
        assert personal_data["user"]["openid"] == "e2e-user"
        assert personal_data["follows"][0]["name"] == "高企延期通知（已改名）"
        assert personal_data["subscriptions"][0]["status"] == "sent"
        assert personal_data["feedback"][0]["content"].endswith("［已隐去］")

        assert client.delete("/api/v1/me").status_code == 204
        for model in (User, Follow, Subscription, Feedback, Event):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        assert client.get("/api/v1/me").json()["agreed"] is None
    finally:
        app.dependency_overrides.clear()
