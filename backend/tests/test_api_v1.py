from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from youju.api import v1
from youju.api.app import app
from youju.db import get_session
from youju.dedup import reconcile_documents
from youju.models import (
    Document,
    DocumentAttachment,
    DocumentDeadline,
    DocumentDetail,
    ListEntry,
    Source,
)

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
    r = client.get("/api/v1/search", params={"status": "公布", "time": "不限"}).json()
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


def test_calendar_and_home_merge_duplicate_notice(client, seeded, session):
    original = session.get(Document, seeded["apply"])
    duplicate = _doc(original.source, 88, original.title, original.publish_date)
    session.add(duplicate)
    session.flush()
    session.add(
        DocumentDeadline(
            document_id=duplicate.id,
            fingerprint="duplicate",
            kind="企业申报截止",
            candidate_date=date(2026, 10, 18),
            approved_date=date(2026, 10, 18),
            evidence="申报截止10月18日",
            confidence="高",
            status="approved",
        )
    )
    reconcile_documents(session)
    session.commit()
    body = client.get("/api/v1/calendar").json()
    assert body["total"] == 3
    assert len([i for i in body["items"] if i["docId"] == str(original.id)]) == 2
    assert client.get("/api/v1/home").json()["stats"]["week_total"] == 3


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


def test_lists_lookup_exact_similar_and_coverage(client, seeded, session):
    list_doc = session.query(Document).filter(Document.title.contains("拟入库科技型")).one()
    common = {"category": "科技型中小企业", "batch": "第4批"}
    session.add_all(
        [
            ListEntry(
                document_id=list_doc.id,
                source_url=list_doc.url,
                name="山东星河科技有限公司",
                normalized_name="山东星河科技有限公司",
                credit_code="91310117588657256C",
                sequence="12",
                **common,
            ),
            # 同一份名单的附件里又出现一次：只列一次
            ListEntry(
                document_id=list_doc.id,
                source_url="https://kjt.shandong.gov.cn/a.xlsx",
                name="山东星河科技有限公司",
                normalized_name="山东星河科技有限公司",
                sequence="12",
                **common,
            ),
            ListEntry(
                document_id=list_doc.id,
                source_url=list_doc.url,
                name="山东星河科技（青岛）有限公司",
                normalized_name="山东星河科技(青岛)有限公司",
                sequence="13",
                **common,
            ),
        ]
    )
    session.commit()
    url = "/api/v1/lists/lookup"
    body = client.get(url, params={"q": "山东星河科技有限公司"}).json()
    assert [i["id"] for i in body["items"]] == [str(list_doc.id)]
    item = body["items"][0]
    assert item["kind"] == "名单" and item["status"] == "公示"
    assert item["matched"] == {
        "name": "山东星河科技有限公司",
        "category": "科技型中小企业",
        "batch": "第4批",
        "sequence": "12",
        "creditCode": "91310117588657256C",
        "region": None,
        "via": "name",
    }
    # 名称相近的另列，不当作同一家
    assert body["similar"] == [{"name": "山东星河科技（青岛）有限公司", "lists": 1}]
    assert body["coverage"] == {"lists": 1, "since": "2026-09-24"}
    # 全角括号、空格不同也能查到；信用代码不分大小写
    assert len(client.get(url, params={"q": "山东星河科技（青岛） 有限公司"}).json()["items"]) == 1
    assert len(client.get(url, params={"q": "91310117588657256c"}).json()["items"]) == 1
    # 查不到：结果为空，收录范围照给，由小程序说明"已收录的名单中未找到"
    missing = client.get(url, params={"q": "不存在的科技有限公司"}).json()
    assert missing["items"] == [] and missing["coverage"]["lists"] == 1
    # 只查企业和机构
    refused = client.get(url, params={"q": "张三"})
    assert refused.status_code == 422 and "只查企业和机构" in refused.json()["detail"]
    assert body["ambiguous"] is False and body["truncated"] is False
    assert (
        item["matched"]["via"] == "name" and item["matched"]["creditCode"] == "91310117588657256C"
    )


def test_lists_lookup_rename_same_name_and_duplicate_notice(client, seeded, session):
    """更名：信用代码相同、名称不同的也列出；同名不同代码：提示可能不是同一家；重复通知只列一次。"""
    list_doc = session.get(Document, seeded["list"])
    src = list_doc.source
    old_doc = _doc(src, 11, "山东省2025年第9批入库科技型中小企业名单公告", date(2025, 11, 3))
    dup_doc = _doc(src, 12, list_doc.title, list_doc.publish_date)  # 同一通知，网址不同
    other = _doc(src, 13, "2026年度高新技术企业认定名单公告", date(2026, 9, 1))
    # 别的机关同一天发了同名通知：不是同一份，不能合并
    ln_src = session.query(Source).filter_by(province="辽宁").one()
    same_title_other_agency = _doc(ln_src, 14, list_doc.title, list_doc.publish_date)
    session.add_all([old_doc, dup_doc, other, same_title_other_agency])
    session.flush()
    code = "91310117588657256C"
    rows = [
        (list_doc, "山东星河科技有限公司", code),
        (dup_doc, "山东星河科技有限公司", code),
        (old_doc, "济南星河科技有限公司", code),  # 更名前
        (other, "山东星河科技有限公司", "91370100MA3C5LTN6Q"),  # 同名、代码不同
        (same_title_other_agency, "山东星河科技有限公司", code),
    ]
    for i, (doc, name, credit) in enumerate(rows):
        session.add(
            ListEntry(
                document_id=doc.id,
                source_url=doc.url,
                name=name,
                normalized_name=name,
                credit_code=credit,
                sequence=str(i + 1),
                category="科技型中小企业",
            )
        )
    session.commit()
    reconcile_documents(session)  # 按内容归并：同一机关同名同日的两个网址归为一份
    session.commit()
    body = client.get("/api/v1/lists/lookup", params={"q": "山东星河科技有限公司"}).json()
    titles = [i["title"] for i in body["items"]]
    # 同一机关的重复通知只列一次；别的机关的同名通知单独列出
    assert titles.count(list_doc.title) == 2
    regions = sorted(i["region"] for i in body["items"] if i["title"] == list_doc.title)
    assert regions == ["山东 · 省级", "辽宁 · 省级"]
    assert old_doc.title in titles and other.title in titles
    renamed = next(i for i in body["items"] if i["title"] == old_doc.title)
    assert renamed["matched"]["name"] == "济南星河科技有限公司"
    assert renamed["matched"]["via"] == "creditCode"
    assert body["ambiguous"] is True
    # 更名前的名称已在结果里，不再算"名称相近"
    assert body["similar"] == []


def test_lists_lookup_shows_master_even_if_its_source_is_not_parsed(client, seeded, session):
    """名单只从可解析的来源（rights_level=1）抽取；条目所在副本归并到别的来源的主文件时，仍列出主文件。"""
    from youju.models import DocumentMerge

    list_doc = session.get(Document, seeded["list"])
    other_src = Source(
        level="省级",
        province="山东",
        dept="科技",
        dept_full_name="山东省科学技术厅（转载）",
        column_name="转载",
        url="https://www.shandong.gov.cn/kj/",
        enabled=True,
        parser_config={},
        rights_level=0,
    )
    session.add(other_src)
    session.flush()
    master = _doc(other_src, 51, list_doc.title, list_doc.publish_date)
    session.add(master)
    session.flush()
    # 主文件编号更小时才会成为主文件；这里直接写归并关系：list_doc 是副本，master 是主文件
    session.add(
        DocumentMerge(duplicate_id=list_doc.id, canonical_id=master.id, active=True, match_key="k")
    )
    session.add(
        ListEntry(
            document_id=list_doc.id,
            source_url=list_doc.url,
            name="山东星河科技有限公司",
            normalized_name="山东星河科技有限公司",
            sequence="1",
            category="科技型中小企业",
        )
    )
    session.commit()
    body = client.get("/api/v1/lists/lookup", params={"q": "山东星河科技有限公司"}).json()
    assert [i["id"] for i in body["items"]] == [str(master.id)]
    assert body["items"][0]["matched"]["name"] == "山东星河科技有限公司"


def test_deadline_change_hides_old_deadline_and_links_new_notice(
    client, seeded, session, monkeypatch
):
    """验收清单 A7：发了延期通知后，原通知的截止标为已变更，公开端不再当作有效截止。"""
    from types import SimpleNamespace

    monkeypatch.setattr("youju.api.app.get_settings", lambda: SimpleNamespace(admin_token="secret"))
    headers = {"X-Admin-Token": "secret"}
    old = session.get(Document, seeded["apply"])
    main = (
        session.query(DocumentDeadline)
        .filter_by(document_id=old.id, kind="企业申报截止", status="approved")
        .one()
    )
    new = _doc(old.source, 9, "关于延长2026年三季度贷款贴息项目申报时间的通知", date(2026, 9, 26))
    session.add(new)
    session.commit()

    found = client.get(
        "/api/admin/deadlines/approved", params={"q": "贷款贴息 三季度"}, headers=headers
    ).json()
    assert main.id in [f["id"] for f in found]
    url = f"/api/admin/deadlines/{main.id}/change"
    body = {"reviewed_by": "审阅人", "reason": "延期", "new_document_id": new.id}
    assert client.post(url, json=body).status_code == 401
    # 必须关联发布变更的新通知；原因只能是三种之一；新通知不能是自己
    no_link = {"reviewed_by": "审阅人", "reason": "撤销"}
    assert client.post(url, json=no_link, headers=headers).status_code == 422
    bad_reason = {**body, "reason": "推迟"}
    assert client.post(url, json=bad_reason, headers=headers).status_code == 422
    self_link = {**body, "new_document_id": old.id}
    assert client.post(url, json=self_link, headers=headers).status_code == 422
    # 防止关联到无关文件：别的地区、或发布早于原通知的，不行
    other_region = session.get(Document, seeded["list"])  # 山东的文件，原通知在辽宁
    wrong = {**body, "new_document_id": other_region.id}
    assert client.post(url, json=wrong, headers=headers).status_code == 422
    earlier = _doc(old.source, 10, "关于调整贷款贴息项目有关事项的通知", date(2026, 9, 1))
    session.add(earlier)
    session.commit()
    too_early = {**body, "new_document_id": earlier.id}
    assert client.post(url, json=too_early, headers=headers).status_code == 422
    undated = _doc(old.source, 11, "关于延长贷款贴息项目申报时间的补充通知", None)
    session.add(undated)
    session.commit()
    no_date = {**body, "new_document_id": undated.id}
    assert client.post(url, json=no_date, headers=headers).status_code == 422
    assert client.post(url, json=body, headers=headers).status_code == 200
    assert client.post(url, json=body, headers=headers).status_code == 409

    detail = client.get(f"/api/v1/documents/{old.id}").json()
    # 原来的企业申报截止不再作为主截止，剩下的推荐截止照常显示
    assert detail["deadline"]["label"] == "推荐或审核截止"
    assert detail["deadline_changes"] == [
        {
            "label": "企业申报截止",
            "date": "2026-10-18",
            "time": "",
            "reason": "延期",
            "newDocId": str(new.id),
            "newTitle": new.title,
        }
    ]
    calendar = client.get("/api/v1/calendar").json()["items"]
    assert not [i for i in calendar if i["docId"] == str(old.id) and i["due"] == "2026-10-18"]

    # 标错了可以撤回：恢复为已通过，变更记录清空
    back = f"/api/admin/deadlines/{main.id}/unchange"
    assert client.post(back, json={"reviewed_by": "审阅人"}, headers=headers).status_code == 200
    assert client.get(f"/api/v1/documents/{old.id}").json()["deadline_changes"] == []
    session.refresh(main)
    assert main.status == "approved" and main.changed_by is None and main.changed_at is None
    # 撤销也要关联撤销通知
    revoke = {"reviewed_by": "审阅人", "reason": "撤销", "new_document_id": new.id}
    assert client.post(url, json=revoke, headers=headers).status_code == 200
    changes = client.get(f"/api/v1/documents/{old.id}").json()["deadline_changes"]
    assert changes[0]["reason"] == "撤销" and changes[0]["newDocId"] == str(new.id)
    docs = client.get("/api/admin/documents", params={"q": "延长 贷款贴息"}, headers=headers)
    assert [d["id"] for d in docs.json()][0] == new.id  # 按发布日期从新到旧，日期不详的排后


def test_deadline_change_covers_merged_twins(client, seeded, session, monkeypatch):
    """同一通知的两个网址归并后，各有一条相同的已通过截止：标记变更时两条一并处理，公开端不再显示。"""
    from types import SimpleNamespace

    from youju.models import DocumentMerge

    monkeypatch.setattr("youju.api.app.get_settings", lambda: SimpleNamespace(admin_token="secret"))
    headers = {"X-Admin-Token": "secret"}
    old = session.get(Document, seeded["apply"])
    twin = _doc(old.source, 21, old.title, old.publish_date)
    new = _doc(old.source, 22, "关于延长2026年三季度贷款贴息项目申报时间的通知", date(2026, 9, 26))
    session.add_all([twin, new])
    session.flush()
    session.add(
        DocumentMerge(duplicate_id=twin.id, canonical_id=old.id, active=True, match_key="k")
    )
    main = (
        session.query(DocumentDeadline)
        .filter_by(document_id=old.id, kind="企业申报截止", status="approved")
        .one()
    )
    copy = DocumentDeadline(
        document_id=twin.id,
        fingerprint="twin",
        kind=main.kind,
        candidate_date=main.candidate_date,
        evidence=main.evidence,
        confidence=main.confidence,
        status="approved",
        reviewed_by="创始人",
        reviewed_at=main.reviewed_at,
        approved_date=main.approved_date,
        approved_time=main.approved_time,
    )
    session.add(copy)
    session.commit()
    # 核对台搜索只列一条
    found = client.get("/api/admin/deadlines/approved", params={"q": "贷款贴息"}, headers=headers)
    assert len([f for f in found.json() if f["kind"] == "企业申报截止"]) == 1
    # 新通知不能是原通知的另一个网址
    body = {"reviewed_by": "审阅人", "reason": "延期", "new_document_id": twin.id}
    url = f"/api/admin/deadlines/{copy.id}/change"
    assert client.post(url, json=body, headers=headers).status_code == 422
    body["new_document_id"] = new.id
    assert client.post(url, json=body, headers=headers).status_code == 200
    session.refresh(main)
    session.refresh(copy)
    assert main.status == copy.status == "changed"
    detail = client.get(f"/api/v1/documents/{old.id}").json()
    assert detail["deadline"]["label"] == "推荐或审核截止"
    assert [c["reason"] for c in detail["deadline_changes"]] == ["延期"]
    # 撤回也一并恢复
    back = f"/api/admin/deadlines/{main.id}/unchange"
    assert client.post(back, json={"reviewed_by": "审阅人"}, headers=headers).status_code == 200
    session.refresh(copy)
    assert copy.status == "approved"
