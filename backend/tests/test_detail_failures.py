"""补详情连续失败、停止自动重试的文件在管理台列出来（D8）。

线上 #56869–#56871 三份文件：10/16 06:31、10/16 20:32、10/18 06:31 各返回一次 HTTP 552，
第 3 次后就没再试（运行日志核对过），只是停了以后哪里都看不到。
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from youju.api import app as admin
from youju.crawler.detail_service import (
    MAX_ATTEMPTS,
    REFETCH_FAILURE_PREFIX,
    fetch_details,
    stopped_retrying,
)
from youju.crawler.fetcher import FetchResult
from youju.db import get_session
from youju.models import DocumentDetail

from .test_detail_service import FakeFetcher, add_document

HEADERS = {"X-Admin-Token": "details-test-token"}
SAMPLE = (
    "<html><body><div id='zoom'><p>这是公开通知正文，需要保存这些段落内容。"
    "申报单位应当按照规定提交材料，主管部门将依法审核并公告结果。</p></div></body></html>"
)


@pytest.fixture
def client(session_factory, monkeypatch):
    def override():
        with session_factory() as session:
            yield session

    monkeypatch.setattr(
        admin,
        "get_settings",
        lambda: SimpleNamespace(
            role="all", admin_token=HEADERS["X-Admin-Token"], advisor_enabled=False
        ),
    )
    admin.app.dependency_overrides[get_session] = override
    with TestClient(admin.app) as client:
        yield client
    admin.app.dependency_overrides.clear()


def run_rounds(session, doc, responses, rounds=MAX_ATTEMPTS) -> FakeFetcher:
    """连跑几轮补详情；每轮之间把排好的重试时间拨到现在，相当于等够了 6 小时、24 小时。"""
    fake = FakeFetcher({doc.url: responses})
    for _ in range(rounds):
        fetch_details(session, limit=10, document_ids=[doc.id], fetcher=fake)
        detail = session.get(DocumentDetail, doc.id)
        if detail is not None and detail.next_retry_at is not None:
            detail.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()
    return fake


def stopped_ids(session) -> set[int]:
    return set(session.scalars(select(DocumentDetail.document_id).where(stopped_retrying())))


def http_552(url: str) -> FetchResult:
    return FetchResult(url, "http_error", final_url=url, http_status=552, error="HTTP 552")


def test_only_temporary_failures_that_used_up_retries_are_listed(session) -> None:
    exhausted = add_document(session, 1)
    fake = run_rounds(session, exhausted, http_552(exhausted.url))
    # 和线上一样：试满 3 次就不再试，再跑几轮也不会选中
    assert len(fake.calls) == MAX_ATTEMPTS == 3
    assert fetch_details(session, limit=10, fetcher=fake) == []
    assert len(fake.calls) == 3

    waiting = add_document(session, 2, source_id=exhausted.source_id)
    run_rounds(session, waiting, FetchResult(waiting.url, "network_error", error="超时"), rounds=1)
    recovered = add_document(session, 3, source_id=exhausted.source_id)
    run_rounds(
        session,
        recovered,
        [
            FetchResult(recovered.url, "network_error", error="超时"),
            FetchResult(recovered.url, "http_error", http_status=503, error="HTTP 503"),
            FetchResult(recovered.url, "ok", http_status=200, text=SAMPLE),
        ],
    )
    gone = add_document(session, 4, source_id=exhausted.source_id)
    run_rounds(session, gone, FetchResult(gone.url, "http_error", http_status=404), rounds=1)
    refused = add_document(session, 5, source_id=exhausted.source_id)
    run_rounds(session, refused, FetchResult(refused.url, "robots_disallowed"), rounds=1)
    robots_down = add_document(session, 6, source_id=exhausted.source_id)
    run_rounds(session, robots_down, FetchResult(robots_down.url, "robots_error"))
    # 正文早就抓到了、只是补附件的重抓失败：文件有正文，不算
    refetched = add_document(session, 7, source_id=exhausted.source_id)
    session.add(
        DocumentDetail(
            document_id=refetched.id,
            body_text="正文",
            fetched_at=datetime.now(UTC),
            http_status=200,
            status="ok",
            failure_reason=REFETCH_FAILURE_PREFIX + "http_error",
            attempt_count=3,
            next_retry_at=None,
        )
    )
    session.commit()

    assert session.get(DocumentDetail, waiting.id).next_retry_at is not None
    assert session.get(DocumentDetail, recovered.id).status == "ok"
    assert stopped_ids(session) == {exhausted.id, robots_down.id}


def test_rows_without_the_full_attempts_or_still_scheduled_are_not_listed(session) -> None:
    # 历史数据或人工改过的行：没试满次数、或者还排着重试时间的，都不算"已停止重试"
    now = datetime.now(UTC)
    first = add_document(session, 8)
    ids = {}
    for number, attempts, retry_at in ((8, 2, None), (9, 3, now), (10, MAX_ATTEMPTS, None)):
        row = first if number == 8 else add_document(session, number, source_id=first.source_id)
        ids[number] = row.id
        session.add(
            DocumentDetail(
                document_id=row.id,
                body_text="",
                fetched_at=now,
                http_status=503,
                status="http_error",
                failure_reason="HTTP 503",
                attempt_count=attempts,
                next_retry_at=retry_at,
            )
        )
    session.commit()
    assert stopped_ids(session) == {ids[10]}


def test_admin_lists_stopped_documents_with_site_and_reason(client, session) -> None:
    first = add_document(session, 11, title="关于组织申报第一项专项资金的通知")
    run_rounds(session, first, http_552(first.url))
    second = add_document(session, 12, source_id=first.source_id)
    second.url = "https://kjt.hubei.gov.cn/detail/12.html"
    second.url_key = "kjt.hubei.gov.cn/detail/12.html"
    session.commit()
    run_rounds(session, second, FetchResult(second.url, "network_error", error="连接失败：超时"))
    pending = add_document(session, 13, source_id=first.source_id)
    run_rounds(session, pending, http_552(pending.url), rounds=1)
    third = add_document(session, 14, source_id=first.source_id)
    run_rounds(session, third, http_552(third.url))

    assert client.get("/api/admin/detail-failures").status_code == 401
    response = client.get("/api/admin/detail-failures", headers=HEADERS)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["total"] == 3 and data["max_attempts"] == 3
    # 最近一次尝试在前
    assert [i["document_id"] for i in data["items"]] == [third.id, second.id, first.id]
    item = data["items"][2]
    assert item == {
        "document_id": first.id,
        "title": "关于组织申报第一项专项资金的通知",
        "url": first.url,
        "publish_date": None,
        "source": "辽宁 · 辽宁省工业和信息化厅 · 通知公告",
        "status": "http_error",
        "http_status": 552,
        "failure_reason": "HTTP 552",
        "attempts": 3,
        "last_attempt_at": item["last_attempt_at"],
    }
    assert datetime.fromisoformat(item["last_attempt_at"]) <= datetime.now(UTC)
    assert data["items"][1]["failure_reason"] == "连接失败：超时"
    # 按网站汇总全部停止重试的文件，多的在前；还排着重试的不算
    assert [(h["host"], h["count"]) for h in data["hosts"]] == [
        ("gxt.ln.gov.cn", 2),
        ("kjt.hubei.gov.cn", 1),
    ]

    page = client.get("/api/admin/detail-failures?limit=1&offset=2", headers=HEADERS).json()
    assert page["total"] == 3 and [i["document_id"] for i in page["items"]] == [first.id]
    # 分页不影响按网站汇总
    assert page["hosts"] == data["hosts"]

    overview = client.get("/api/admin/overview", headers=HEADERS).json()
    assert overview["todos"]["detail_failures"] == 3


def test_details_page_is_static_and_needs_the_token_for_data(client) -> None:
    page = client.get("/admin/details")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "/api/admin/detail-failures" in page.text and "youjuAdminToken()" in page.text
    assert "连续" in page.text and "不再自动重试" in page.text
    portal = client.get("/admin").text
    assert 'data-menu="details"' in portal and "'detail_failures','details'" in portal
