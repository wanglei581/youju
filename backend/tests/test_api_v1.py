from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from youju.api import v1
from youju.api.app import app
from youju.db import get_session
from youju.models import Document, DocumentAttachment, DocumentDeadline, DocumentDetail, Source

# 北京时间 2026-09-27（周日）上午 10 点；本周是 9/21（周一）到 9/28（下周一，不含）
NOW = datetime(2026, 9, 27, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def client(session_factory, monkeypatch):
    def override():
        with session_factory() as s:
            yield s

    monkeypatch.setattr(v1, "now", lambda: NOW)
    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def _doc(src, n, title, day):
    now = datetime.now(UTC)
    return Document(
        source_id=src.id,
        url=f"https://{src.url.split('/')[2]}/{n}.html",
        url_key=f"{src.url.split('/')[2]}/{n}.html",
        title=title,
        publish_date=day,
        first_seen_at=now,
        last_seen_at=now,
    )


@pytest.fixture
def seeded(session):
    ln = Source(
        level="省级",
        province="辽宁",
        dept="工信",
        dept_full_name="辽宁省工业和信息化厅",
        column_name="通知",
        url="https://gxt.ln.gov.cn/tz/",
        enabled=True,
        parser_config={},
    )
    sd = Source(
        level="省级",
        province="山东",
        dept="科技",
        dept_full_name="山东省科学技术厅",
        column_name="通知",
        url="https://kjt.shandong.gov.cn/tz/",
        enabled=True,
        parser_config={},
    )
    gj = Source(
        level="国家级",
        province="国家",
        dept="统计",
        dept_full_name="国家统计局",
        column_name="发布",
        url="https://www.stats.gov.cn/fb/",
        enabled=True,
        parser_config={},
    )
    session.add_all([ln, sd, gj])
    session.flush()
    apply_doc = _doc(
        ln,
        1,
        "辽宁省工业和信息化厅关于组织开展2026年三季度贷款贴息项目申报工作的通知",
        date(2026, 9, 24),
    )
    list_doc = _doc(sd, 2, "山东省2026年第4批拟入库科技型中小企业名单公示", date(2026, 9, 24))
    policy_doc = _doc(
        sd, 3, "山东省科学技术厅关于印发《科技计划项目管理办法》的通知", date(2026, 8, 1)
    )
    stat_doc = _doc(gj, 4, "2026年9月中旬流通领域重要生产资料市场价格变动情况", date(2026, 9, 26))
    future_doc = _doc(gj, 5, "2026年9月下旬流通领域重要生产资料市场价格变动情况", date(2026, 9, 28))
    session.add_all([apply_doc, list_doc, policy_doc, stat_doc, future_doc])
    session.flush()
    session.add(
        DocumentDetail(
            document_id=apply_doc.id,
            body_text="……",
            issuer="辽宁省工业和信息化厅",
            doc_number="辽工信投资〔2026〕130号",
            fetched_at=datetime.now(UTC),
            status="ok",
            attachments=[
                DocumentAttachment(
                    position=0,
                    name="附件1 实施细则",
                    url="https://gxt.ln.gov.cn/a1.pdf",
                    extension="pdf",
                )
            ],
        )
    )
    reviewed = datetime(2026, 9, 26, 3, 0, tzinfo=UTC)
    session.add_all(
        [
            DocumentDeadline(
                document_id=apply_doc.id,
                fingerprint="a",
                kind="企业申报截止",
                candidate_date=date(2026, 10, 18),
                evidence="企业申报截止时间为10月18日",
                confidence="高",
                status="approved",
                reviewed_by="创始人",
                reviewed_at=reviewed,
                approved_date=date(2026, 10, 18),
            ),
            DocumentDeadline(
                document_id=apply_doc.id,
                fingerprint="b",
                kind="推荐或审核截止",
                candidate_date=date(2026, 10, 20),
                evidence="市级推荐截止10月20日",
                confidence="高",
                status="approved",
                reviewed_by="创始人",
                reviewed_at=reviewed,
                approved_date=date(2026, 10, 20),
                approved_time=time(17, 0),
            ),
            # 当天 9 点已截止（现在 10 点）：日历里不能出现
            DocumentDeadline(
                document_id=policy_doc.id,
                fingerprint="d",
                kind="材料报送截止",
                candidate_date=date(2026, 9, 27),
                evidence="9月27日9:00前报送",
                confidence="高",
                status="approved",
                reviewed_by="创始人",
                reviewed_at=reviewed,
                approved_date=date(2026, 9, 27),
                approved_time=time(9, 0),
            ),
            # 当天 17 点截止：还没过，日历里要有
            DocumentDeadline(
                document_id=policy_doc.id,
                fingerprint="e",
                kind="企业申报截止",
                candidate_date=date(2026, 9, 27),
                evidence="9月27日17:00前提交",
                confidence="高",
                status="approved",
                reviewed_by="创始人",
                reviewed_at=reviewed,
                approved_date=date(2026, 9, 27),
                approved_time=time(17, 0),
            ),
            # 没核对的候选：任何公开接口都不能出现
            DocumentDeadline(
                document_id=list_doc.id,
                fingerprint="c",
                kind="企业申报截止",
                candidate_date=date(2026, 10, 5),
                evidence="10月5日前",
                confidence="中",
                status="pending",
            ),
        ]
    )
    session.commit()
    return {
        "apply": apply_doc.id,
        "list": list_doc.id,
        "policy": policy_doc.id,
        "stat": stat_doc.id,
    }


def test_home_counts_week_and_skips_other_kinds_in_latest(client, seeded):
    body = client.get("/api/v1/home").json()
    assert body["stats"]["week_total"] == 3  # 9/24、9/24、9/26；8/1 的政策不算本周
    assert (
        body["stats"]["apply"] == 1 and body["stats"]["lists"] == 1 and body["stats"]["other"] == 1
    )
    assert body["stats"]["provinces"] == 2 and body["stats"]["sources"] == 3
    kinds = [i["kind"] for i in body["items"]]
    assert "其他" not in kinds
    apply_item = next(i for i in body["items"] if i["id"] == str(seeded["apply"]))
    assert apply_item["deadline"] == {"date": "2026-10-18", "time": "", "verified": True}


def test_search_filters_and_totals(client, seeded):
    r = client.get("/api/v1/search", params={"q": "科技型中小企业"}).json()
    assert [i["id"] for i in r["items"]] == [str(seeded["list"])]
    assert r["items"][0]["status"] == "公示"
    assert r["items"][0]["deadline"] is None  # 只有 pending 候选，不能显示
    r = client.get("/api/v1/search", params={"types": "政策文件", "time": "近 30 天"}).json()
    assert r["total"] == 0 and r["total_all"] == 1  # 8/1 的政策超出近 30 天
    r = client.get("/api/v1/search", params={"regions": "国家部委", "time": "不限"}).json()
    assert r["total"] == 2 and r["items"][0]["region"] == "国家"
    r = client.get("/api/v1/search", params={"depts": "工信部门", "time": "不限"}).json()
    assert [i["id"] for i in r["items"]] == [str(seeded["apply"])]
    r = client.get("/api/v1/search", params={"q": "辽工信投资", "time": "不限"}).json()
    assert r["total"] == 1  # 文号也能搜到
    r = client.get("/api/v1/search", params={"status": "正式公布", "time": "不限"}).json()
    assert r["total"] == 0


def test_document_detail_only_approved_deadlines(client, seeded):
    d = client.get(f"/api/v1/documents/{seeded['apply']}").json()
    assert d["deadline"]["label"] == "企业申报截止" and d["deadline"]["date"] == "2026-10-18"
    assert d["deadline"]["notes"] == ["推荐或审核截止 10月20日 周二 17:00"]
    assert ["文号", "辽工信投资〔2026〕130号"] in d["points"]
    assert d["attachments"] == [
        {"ext": "PDF", "name": "附件1 实施细则", "url": "https://gxt.ln.gov.cn/a1.pdf"}
    ]
    assert d["checked"] == "2026-09-26" and d["host"] == "gxt.ln.gov.cn"
    listed = client.get(f"/api/v1/documents/{seeded['list']}").json()
    assert listed["deadline"] is None and listed["excerpt"] is None
    assert client.get("/api/v1/documents/999999").status_code == 404


def test_calendar_lists_only_approved_upcoming(client, seeded):
    items = client.get("/api/v1/calendar").json()["items"]
    assert [i["due"] for i in items] == ["2026-09-27", "2026-10-18", "2026-10-20"]
    assert all(i["verified"] for i in items)
    assert items[2]["time"] == "17:00" and items[1]["region"] == "辽宁 · 工信"


def test_recent_lists(client, seeded):
    body = client.get("/api/v1/lists/recent").json()
    assert body["total"] == 1 and body["week_total"] == 1
    assert body["items"][0]["kind"] == "名单"


def test_home_week_has_upper_bound_and_followed_new_is_null(client, seeded):
    stats = client.get("/api/v1/home").json()["stats"]
    assert stats["week_total"] == 3  # 9/28（下周一）的文件不算本周
    assert stats["followed_new"] is None


def test_calendar_skips_deadlines_already_passed_today(client, seeded):
    body = client.get("/api/v1/calendar").json()
    dues = [(i["due"], i["time"]) for i in body["items"]]
    assert ("2026-09-27", "09:00") not in dues
    assert dues[0] == ("2026-09-27", "17:00")
    assert body["total"] == len(body["items"]) == 3
    assert all(i["docId"] and i["id"].startswith("dl") for i in body["items"])


@pytest.mark.parametrize(
    "params",
    [
        {"types": "公告"},
        {"regions": "火星"},
        {"status": "已撤销"},
        {"depts": "财政部门"},
        {"kind": "新闻"},
        {"time": "近 90 天"},
        {"offset": 5001},
        {"regions": ",".join(["北京"] * 41)},
    ],
)
def test_search_rejects_invalid_filters(client, seeded, params):
    assert client.get("/api/v1/search", params=params).status_code == 422
