"""转发组对外展示（D-055 第二步）：只有确认过的组才按组折叠、按组计数；截止按版本标，不互相套用。"""

from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from youju.api import app as admin
from youju.api import v1
from youju.db import get_session
from youju.dedup import reconcile_documents
from youju.models import (
    Document,
    DocumentDeadline,
    DocumentDetail,
    RepostGroupMember,
    Source,
)
from youju.repost import rebuild

# 北京时间 2026-09-27（周日）；本周是 9/21–9/27
NOW = datetime(2026, 9, 27, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
HEADERS = {"X-Admin-Token": "repost-public-token"}
UPSTREAM = (
    "根据《工业和信息化部等七部门办公厅（办公室）关于组织开展2026年度视听系统典型案例征集工作的通知》"
    "（工信厅联电子函〔2026〕376号）要求，现就有关工作通知如下。"
)
TITLE = "关于组织开展2026年度视听系统典型案例征集工作的通知"


@pytest.fixture
def client(session_factory, monkeypatch):
    def override():
        with session_factory() as s:
            yield s

    monkeypatch.setattr(v1, "now", lambda: NOW)
    monkeypatch.setattr(
        admin,
        "get_settings",
        lambda: SimpleNamespace(
            role="all", admin_token=HEADERS["X-Admin-Token"], advisor_enabled=False
        ),
    )
    admin.app.dependency_overrides[get_session] = override
    with TestClient(admin.app) as test_client:
        yield test_client
    admin.app.dependency_overrides.clear()


def _source(session, province, dept="工信"):
    src = Source(
        level="国家级" if province == "国家" else "省级",
        province=province,
        dept=dept,
        dept_full_name="工业和信息化部" if province == "国家" else f"{province}省工业和信息化厅",
        column_name="通知公告",
        url=f"https://{province}-{dept}.example.gov.cn/tzgg/",
        enabled=True,
        parser_config={},
        rights_level=1,
    )
    session.add(src)
    session.flush()
    return src


def _doc(session, province, title=TITLE, day=22, body=UPSTREAM):
    src = _source(session, province)
    stamp = datetime.now(UTC)
    doc = Document(
        source_id=src.id,
        url=f"https://{province}.example.gov.cn/{day}.html",
        url_key=f"{province}.example.gov.cn/{day}.html",
        title=title,
        publish_date=date(2026, 9, day),
        first_seen_at=stamp,
        last_seen_at=stamp,
    )
    session.add(doc)
    session.flush()
    session.add(
        DocumentDetail(
            document_id=doc.id,
            body_text=body + province,
            fetched_at=stamp,
            status="ok",
            http_status=200,
        )
    )
    session.flush()
    return doc


def _deadline(session, doc, kind, day, status="approved", clock=None):
    row = DocumentDeadline(
        document_id=doc.id,
        fingerprint=f"{doc.id}-{kind}-{day}",
        kind=kind,
        candidate_date=date(2026, 10, day),
        candidate_time=clock,
        evidence=f"请于10月{day}日前报送",
        confidence="高",
        status=status,
    )
    if status == "approved":
        row.reviewed_by = "创始人"
        row.reviewed_at = datetime(2026, 9, 26, tzinfo=UTC)
        row.approved_date = row.candidate_date
        row.approved_time = clock
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def world(session):
    """视听系统典型案例三个省的版本（同一份部里通知），各自的截止不同；另有一份不相干的通知。"""
    docs = {
        "shaanxi": _doc(session, "陕西", day=22),
        "tianjin": _doc(session, "天津", day=23),
        "fujian": _doc(
            session,
            "福建",
            "福建省工业和信息化厅等六部门关于开展2026年度视听系统典型案例征集工作的通知",
            day=24,
        ),
        "jiangsu": _doc(session, "江苏", "关于组织申报2026年度省级工业设计中心的通知", 25, "江苏"),
    }
    _deadline(session, docs["shaanxi"], "推荐或审核截止", 10)
    _deadline(session, docs["tianjin"], "企业申报截止", 12, clock=time(16, 0))
    _deadline(session, docs["fujian"], "推荐或审核截止", 15, status="pending")
    session.commit()
    rebuild(session)
    session.commit()
    return docs


def _confirm(session):
    for row in session.scalars(select(RepostGroupMember)):
        row.confirmed_by, row.confirmed_at = "创始人", datetime.now(UTC)
    session.commit()


def _search(client, **params):
    response = client.get("/api/v1/search", params={"time": "不限", **params})
    assert response.status_code == 200
    return response.json()


def test_unconfirmed_group_is_not_folded(client, world) -> None:
    """D-055：前 4 周每组都要确认后才对外合并；没确认的组仍按单条显示、按条计数。"""
    body = _search(client, group=1)
    assert body["total"] == 4 and body["reposts"] == 0
    assert not any("group" in item for item in body["items"])
    stats = client.get("/api/v1/home", params={"group": 1}).json()["stats"]
    assert stats["week_total"] == 4 and stats["week_reposts"] == 0
    detail = client.get(f"/api/v1/documents/{world['tianjin'].id}").json()
    assert "repost" not in detail


def test_confirmed_group_folds_and_counts_by_group(client, session, world) -> None:
    _confirm(session)
    body = _search(client, group=1)
    # 一个组算 1 份：三个省的版本折叠成一条主条目（最早发布的陕西版）
    assert body["total"] == 2 and body["total_all"] == 2 and body["reposts"] == 2
    main = next(item for item in body["items"] if item["id"] == str(world["shaanxi"].id))
    assert main["group"]["versions"] == 2
    assert main["group"]["regions"] == ["天津", "福建"]
    assert [v["label"] for v in main["group"]["items"]] == ["天津版", "福建版"]
    # 每个版本各自的原文链接
    assert main["group"]["items"][0]["url"] == world["tianjin"].url
    # 翻页按主条目：翻到底的条数 = total
    page = _search(client, group=1, limit=1, offset=1)
    assert len(page["items"]) == 1 and page["total"] == 2
    # 首页本周新增按组算：本周有新版本的组计 1 份，另写并进组里的版本数
    home = client.get("/api/v1/home", params={"group": 1}).json()
    assert home["stats"]["week_total"] == 2 and home["stats"]["apply"] == 2
    assert home["stats"]["week_reposts"] == 2
    assert len(home["items"]) == 2
    # 旧版小程序不传 group：返回和以前一样（按条计数、不带 group、reposts）
    old = _search(client)
    assert old["total"] == 4 and "reposts" not in old
    assert not any("group" in item for item in old["items"])
    old_home = client.get("/api/v1/home").json()
    assert old_home["stats"]["week_total"] == 4 and "week_reposts" not in old_home["stats"]
    lists = client.get("/api/v1/lists/recent", params={"group": 1}).json()
    assert lists["total"] == 0


def test_filter_first_then_fold(client, session, world) -> None:
    """先筛选、再折叠：选了福建，主条目是福建版；“另有 N 个”数的是组里其他全部版本。"""
    _confirm(session)
    body = _search(client, group=1, regions="福建")
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == str(world["fujian"].id)
    assert item["group"]["versions"] == 2 and item["group"]["regions"] == ["陕西", "天津"]
    # 关键词只命中某一版时也一样
    body = _search(client, group=1, q="六部门")
    assert body["total"] == 1 and body["items"][0]["group"]["versions"] == 2


def test_deadlines_stay_per_version(client, session, world) -> None:
    """不同版本的截止不互相套用：主条目、详情都只用这一版自己核对过的截止，其他版本按版本标。"""
    _confirm(session)
    main = next(
        item for item in _search(client, group=1)["items"] if item["id"] == str(world["shaanxi"].id)
    )
    assert main["deadline"]["date"] == "2026-10-10"  # 陕西版自己的，不是天津版的 10-12
    assert main["app_status"]["label"] == "推荐或审核截止"

    fujian = client.get(f"/api/v1/documents/{world['fujian'].id}").json()
    # 福建版自己的候选还没核对：不借用陕西、天津已通过的日期
    assert fujian["deadline"] is None and fujian["deadline_state"] == "unverified"
    repost = fujian["repost"]
    assert repost["label"] == "福建版"
    assert repost["upstream"] == {
        "title": "工业和信息化部等七部门办公厅（办公室）关于组织开展2026年度视听系统典型案例征集工作的通知",
        "number": "工信厅联电子函〔2026〕376号",
    }
    versions = {v["label"]: v for v in repost["versions"]}
    assert [v["label"] for v in repost["versions"]] == ["福建版", "陕西版", "天津版"]
    assert versions["福建版"]["current"] is True
    assert versions["福建版"]["deadlines"] == []
    assert versions["福建版"]["deadline_state"] == "unverified"
    assert versions["陕西版"]["deadlines"] == [
        {"label": "推荐或审核截止", "date": "2026-10-10", "time": ""}
    ]
    assert versions["天津版"]["deadlines"] == [
        {"label": "企业申报截止", "date": "2026-10-12", "time": "16:00"}
    ]
    assert versions["天津版"]["url"] == world["tianjin"].url
    # 库里没有部里原件：不出现“部里”
    assert all(v["label"] != "部里" for v in repost["versions"])

    tianjin = client.get(f"/api/v1/documents/{world['tianjin'].id}").json()
    assert tianjin["deadline"]["label"] == "企业申报截止"
    assert tianjin["deadline"]["date"] == "2026-10-12" and tianjin["deadline"]["notes"] == []


def test_national_original_is_main_and_called_ministry(client, session, world) -> None:
    national = _doc(
        session,
        "国家",
        "工业和信息化部等七部门办公厅（办公室）关于组织开展2026年度视听系统典型案例征集工作的通知",
        day=26,
        body="各有关单位：现组织开展2026年度视听系统典型案例征集工作。",
    )
    _deadline(session, national, "企业申报截止", 20)
    session.commit()
    rebuild(session)
    session.commit()
    _confirm(session)
    body = _search(client, group=1)
    # 部里原件在库就用原件做主条目（即使它发布得晚）
    main = next(item for item in body["items"] if item.get("group"))
    assert main["id"] == str(national.id) and main["group"]["versions"] == 3
    detail = client.get(f"/api/v1/documents/{world['shaanxi'].id}").json()
    first = detail["repost"]["versions"][0]
    assert first["label"] == "部里" and first["deadlines"][0]["label"] == "企业申报截止"
    assert detail["repost"]["versions"][1]["label"] == "陕西版"


def test_withdraw_and_remove_restore_single_items(client, session, world) -> None:
    group_id = session.scalars(select(RepostGroupMember.group_id)).first()
    confirm = client.post(
        f"/api/admin/repost-groups/{group_id}/confirm",
        headers=HEADERS,
        json={"reviewed_by": "创始人"},
    )
    assert confirm.status_code == 200
    assert _search(client, group=1)["total"] == 2
    # 撤回确认后马上恢复成单条
    withdraw = client.post(
        f"/api/admin/repost-groups/{group_id}/withdraw",
        headers=HEADERS,
        json={"reviewed_by": "创始人"},
    )
    assert withdraw.status_code == 200
    body = _search(client, group=1)
    assert body["total"] == 4 and not any("group" in item for item in body["items"])
    # 再确认、移出福建版：福建版单独一条，另两版仍是一组
    client.post(
        f"/api/admin/repost-groups/{group_id}/confirm",
        headers=HEADERS,
        json={"reviewed_by": "创始人"},
    )
    client.post(
        f"/api/admin/repost-groups/{group_id}/members/{world['fujian'].id}/remove",
        headers=HEADERS,
        json={"reviewed_by": "创始人"},
    )
    body = _search(client, group=1)
    assert body["total"] == 3
    fujian = next(item for item in body["items"] if item["id"] == str(world["fujian"].id))
    assert "group" not in fujian
    assert "repost" not in client.get(f"/api/v1/documents/{world['fujian'].id}").json()


def test_taken_down_version_is_not_listed(client, session, world) -> None:
    _confirm(session)
    world["tianjin"].taken_down_at = datetime.now(UTC)
    session.commit()
    main = next(
        item for item in _search(client, group=1)["items"] if item["id"] == str(world["shaanxi"].id)
    )
    assert main["group"]["regions"] == ["福建"]


def test_same_url_counts_once(client, session) -> None:
    """网址相同的两条（机关一个取自详情、一个取自来源）归为同一文件，对外只算一份。"""
    src = _source(session, "辽宁")
    stamp = datetime.now(UTC)
    url = "https://gxt.ln.gov.cn/tzgg/2026092511/index.html"
    docs = [
        Document(
            source_id=src.id,
            url=url,
            url_key=key,
            title="辽宁省工业和信息化厅关于2026年度先进制造业企业名单制定工作有关事项的通知",
            publish_date=date(2026, 9, 25),
            first_seen_at=stamp,
            last_seen_at=stamp,
        )
        for key in ("gxt.ln.gov.cn/tzgg/2026092511/index.html", "gxt.ln.gov.cn/tzgg/2026092511/")
    ]
    session.add_all(docs)
    session.flush()
    session.add(
        DocumentDetail(
            document_id=docs[0].id,
            body_text="",
            issuer="辽宁省工业和信息化厅、辽宁省通信管理局",
            fetched_at=stamp,
            status="ok",
        )
    )
    session.commit()
    assert _search(client)["total"] == 2
    reconcile_documents(session)
    session.commit()
    assert _search(client)["total"] == 1 and _search(client, group=1)["total"] == 1
