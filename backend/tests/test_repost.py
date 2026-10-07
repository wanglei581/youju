"""转发归组（D-055）：规则、增量建组、管理台转发归组页、核对台对照；截止日期不随转发组同步。"""

from datetime import UTC, date, datetime, time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from youju.api import app as admin
from youju.db import get_session
from youju.dedup import reconcile_documents, sync_reviewed_twins
from youju.models import (
    Document,
    DocumentDeadline,
    DocumentDetail,
    DocumentMerge,
    RepostGroup,
    RepostGroupMember,
    Source,
)
from youju.repost import (
    Candidate,
    cited_upstream,
    core_title,
    member_facts,
    pair_strength,
    propose_groups,
    rebuild,
    signals,
    similar_titles,
)

HEADERS = {"X-Admin-Token": "repost-test-token"}
NOW = datetime(2026, 9, 20, 2, 0, tzinfo=UTC)
UPSTREAM = (
    "根据《工业和信息化部等七部门办公厅（办公室）关于组织开展2026年度视听系统典型案例征集工作的通知》"
    "（工信厅联电子函〔2026〕376号）要求，现就有关工作通知如下。"
)


# ---- 规则 --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "core"),
    [
        (
            "关于转发国家重点研发计划“战略性科技创新合作”重点专项2026年度联合研发与示范项目申报指南的通知",
            "国家重点研发计划战略性科技创新合作重点专项2026年联合研发与示范项目",
        ),
        (
            "四川省科学技术厅关于组织申报国家重点研发计划“战略性科技创新合作”重点专项2026年度"
            "联合研发与示范项目的通知",
            "国家重点研发计划战略性科技创新合作重点专项2026年联合研发与示范项目",
        ),
        (
            "福建省科学技术厅转发科技部国际合作司关于发布国家重点研发计划“战略性科技创新合作”"
            "重点专项2026年度联合研发与示范项目申报指南的通知",
            "国家重点研发计划战略性科技创新合作重点专项2026年联合研发与示范项目",
        ),
        ("关于组织开展2026年度视听系统典型案例征集工作的通知", "2026年视听系统典型案例"),
        ("北京市经济和信息化局关于征集2026年度视听系统典型案例的通知", "2026年视听系统典型案例"),
        (
            "自治区工业和信息化厅关于继续做好2026年度享受增值税加计抵减政策的先进制造业企业名单"
            "制定工作有关事项的通知",
            "2026年享受增值税加计抵减政策的先进制造业企业名单制定",
        ),
    ],
)
def test_core_title_strips_shell_agency_verbs_and_tails(title, core) -> None:
    assert core_title(title) == core


def test_cited_upstream_only_counts_ministry_notices() -> None:
    body = (
        "根据《工业和信息化部办公厅关于集中组织开展2026年典型案例征集工作的通知》"
        "（工信厅办函［2026］411号），依据《优质中小企业梯度培育管理办法》（工信部企业〔2026〕63号）"
        "和《省工信厅关于印发工作要点的通知》（鲁工信发〔2026〕5号），参照"
        "《关于开展2023年度试点的通知》（工信厅科函〔2023〕9号）。"
    )
    cited = cited_upstream(body, 2026)
    # 只留下部里的申报通知：管理办法（规则文件）、省里自己的文号、太旧的都不算
    assert [c.number for c in cited] == ["工信厅办函〔2026〕411号"]
    assert cited[0].title.startswith("工业和信息化部办公厅")


def test_similar_titles_separates_directions_of_one_notice() -> None:
    assert similar_titles("2026年视听系统典型案例", "2026年视听系统典型案例征集推荐")
    # 工信厅办函〔2026〕411 号一份通知，各省拆成不同方向：不能并成一组（方案 4.4）
    assert not similar_titles("2026年绿色能源应用典型场景与案例", "2026年数字化转型典型案例")


def _cand(doc_id, title, province, *, dept="工信", kind="申报通知", day=10, body=""):
    return Candidate(
        id=doc_id,
        title=title,
        kind=kind,
        province=province,
        dept=dept,
        publish_date=date(2026, 9, day),
        body_head=body,
    )


def _pair(a: Candidate, b: Candidate):
    return pair_strength(a, signals(a), b, signals(b))


def test_pair_rules() -> None:
    forwarded = _cand(1, "关于转发2026年高端医疗装备推广应用项目申报工作的通知", "河北")
    local = _cand(2, "关于开展2026年高端医疗装备推广应用项目申报工作的通知", "天津")
    assert _pair(forwarded, local) == "strong"  # 核心标题相同，一方标题写明转发
    # 同一机关（同省同部门）同名：不同阶段或批次，不归组
    same_agency = _cand(3, "关于开展2026年高端医疗装备推广应用项目申报工作的通知", "河北")
    assert _pair(forwarded, same_agency) is None
    # 相隔超过 90 天
    late = Candidate(**{**local.__dict__, "id": 4, "publish_date": date(2027, 1, 1)})
    assert _pair(forwarded, late) is None
    # 都没有上游证据：疑似，要人确认
    a = _cand(5, "关于组织开展2026年工业和信息化领域创新任务揭榜挂帅申报工作的通知", "北京")
    b = _cand(6, "关于组织开展2026年工业和信息化领域创新任务揭榜挂帅申报工作的通知", "新疆")
    assert _pair(a, b) == "weak"
    # 本地项目（省级……）各地各一份
    c = _cand(7, "关于转发2026年省级企业技术中心认定工作的通知", "江苏")
    d = _cand(8, "关于开展2026年省级企业技术中心认定工作的通知", "青海")
    assert _pair(c, d) is None
    # 名单各地各一份：同名也不归组
    e = _cand(9, "工业和信息化部科技型企业孵化器拟推荐名单公示", "福建", kind="名单")
    f = _cand(10, "工业和信息化部科技型企业孵化器拟推荐名单公示", "宁夏", kind="名单")
    assert _pair(e, f) is None
    # 通知和名单（不同阶段）不归组
    g = _cand(11, "关于转发2026年高端医疗装备推广应用项目申报工作的通知", "山东", kind="名单")
    assert _pair(forwarded, g) is None
    # 都引用同一份部里通知、标题相近：强证据
    h = _cand(12, "关于组织开展2026年度视听系统典型案例征集推荐工作的通知", "陕西", body=UPSTREAM)
    i = _cand(13, "关于开展2026年度视听系统典型案例征集工作的通知", "福建", body=UPSTREAM)
    assert _pair(h, i) == "strong"


def test_propose_groups_chains_and_marks_weak_groups() -> None:
    docs = [
        _cand(1, "关于转发2026年高端医疗装备推广应用项目申报工作的通知", "河北"),
        _cand(2, "关于开展2026年高端医疗装备推广应用项目申报工作的通知", "天津"),
        _cand(3, "市工信局关于开展2026年高端医疗装备推广应用项目申报工作的通知", "重庆"),
        _cand(4, "关于开展人工智能应用服务商培育专项行动的通知", "黑龙江", kind="其他"),
        _cand(5, "关于开展人工智能应用服务商培育专项行动的通知", "四川", kind="其他"),
        _cand(6, "关于开展人工智能应用服务商培育专项行动的通知", "陕西", kind="资讯与指南"),
        _cand(7, "关于开展2026年数字化转型典型案例征集工作的通知", "天津"),
    ]
    groups = propose_groups(docs)
    assert [(g.ids, g.strength) for g in groups] == [([1, 2, 3], "strong"), ([4, 5, 6], "weak")]
    assert groups[0].evidence[1] == "标题转发"


# ---- 数据库 ------------------------------------------------------------------


def _source(session, province, dept="工信", full=None):
    src = Source(
        level="国家级" if province == "国家" else "省级",
        province=province,
        dept=dept,
        dept_full_name=full or f"{province}省{dept}厅",
        column_name="通知公告",
        url=f"https://{province}-{dept}.example.gov.cn/tzgg/",
        enabled=True,
        parser_config={},
    )
    session.add(src)
    session.flush()
    return src


def _doc(session, src, title, *, day=10, body="", key=None):
    key = key or f"{src.url}{title}"
    doc = Document(
        source_id=src.id,
        url=f"https://{key}",
        url_key=key[:400],
        title=title,
        publish_date=date(2026, 9, day),
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    session.add(doc)
    session.flush()
    if body:
        session.add(
            DocumentDetail(
                document_id=doc.id, body_text=body, fetched_at=NOW, status="ok", http_status=200
            )
        )
        session.flush()
    return doc


def _deadline(session, doc, kind="推荐或审核截止", day=30, evidence="请于9月30日前报送", **extra):
    row = DocumentDeadline(
        document_id=doc.id,
        fingerprint=f"{doc.id}-{kind}-{day}-{evidence}",
        kind=kind,
        candidate_date=date(2026, 9, day),
        candidate_time=extra.pop("candidate_time", None),
        evidence=evidence,
        confidence="高",
        **extra,
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def world(session):
    """视听系统典型案例：陕西、天津、福建三省各一版（都引用部里 376 号），另有一份不相干的通知。"""
    title = "关于组织开展2026年度视听系统典型案例征集工作的通知"
    docs = {
        "shaanxi": _doc(session, _source(session, "陕西"), title, day=7, body=UPSTREAM + "陕西"),
        "tianjin": _doc(session, _source(session, "天津"), title, day=14, body=UPSTREAM + "天津"),
        "fujian": _doc(
            session,
            _source(session, "福建"),
            "福建省工业和信息化厅等六部门关于开展2026年度视听系统典型案例征集工作的通知",
            day=24,
            body=UPSTREAM + "福建",
        ),
        "other": _doc(
            session, _source(session, "江苏"), "关于开展2026年度省级工业设计中心认定的通知"
        ),
    }
    session.commit()
    return docs


def _members(session):
    return {
        row.document_id: row
        for row in session.scalars(
            select(RepostGroupMember).execution_options(populate_existing=True)
        )
    }


def test_rebuild_creates_pending_group_and_keeps_human_decisions(session, world) -> None:
    summary = rebuild(session, now=NOW)
    session.commit()
    assert summary.created == 1 and summary.joined == 3
    group = session.scalars(select(RepostGroup)).one()
    assert group.strength == "strong" and group.topic == "2026年视听系统典型案例"
    assert group.upstream_number == "工信厅联电子函〔2026〕376号"
    members = _members(session)
    ids = {world[k].id for k in ("shaanxi", "tianjin", "fujian")}
    assert set(members) == ids
    # 所有组必须人工确认：规则建的组全部待确认
    assert all(row.confirmed_at is None for row in members.values())
    # 人工移出的版本，重跑不会加回
    members[world["fujian"].id].active = False
    members[world["fujian"].id].removed_by = "创始人"
    session.commit()
    rebuild(session, now=NOW)
    session.commit()
    assert _members(session)[world["fujian"].id].active is False
    # 重跑幂等
    again = rebuild(session, now=NOW)
    assert (again.created, again.joined, again.dropped) == (0, 0, 0)


def test_rebuild_joins_new_versions_and_drops_unsupported(session, world) -> None:
    rebuild(session, now=NOW)
    session.commit()
    group = session.scalars(select(RepostGroup)).one()
    _confirm_all(session)
    # 新出现的版本：加入已有的组，待确认（已确认的版本不受影响）
    newcomer = _doc(
        session,
        _source(session, "北京"),
        "北京市经济和信息化局关于征集2026年度视听系统典型案例的通知",
        day=11,
    )
    session.commit()
    rebuild(session, now=NOW)
    session.commit()
    members = _members(session)
    assert members[newcomer.id].group_id == group.id
    assert members[newcomer.id].confirmed_at is None
    assert members[world["tianjin"].id].confirmed_at is not None
    # 规则不再支持（标题改了）：还没确认的自动成员删掉；确认过的保留
    newcomer.title = "北京市经济和信息化局关于征集2026年度虚拟现实应用案例的通知"
    world["tianjin"].kind_override = "名单"
    session.commit()
    summary = rebuild(session, now=NOW)
    session.commit()
    members = _members(session)
    assert newcomer.id not in members and summary.dropped == 1
    assert members[world["tianjin"].id].active is True


def test_rebuild_moves_membership_to_canonical_file(session, world) -> None:
    rebuild(session, now=NOW)
    session.commit()
    # 天津那版后来被认定为同一文件的另一个网址（归并到另一个编号更小的主文件）
    twin = _doc(
        session,
        session.get(Source, world["tianjin"].source_id),
        world["tianjin"].title,
        day=14,
        key="tianjin.example.gov.cn/another-column/1.html",
    )
    session.add(
        DocumentMerge(
            duplicate_id=world["tianjin"].id, canonical_id=twin.id, active=True, match_key="t"
        )
    )
    session.commit()
    summary = rebuild(session, now=NOW)
    session.commit()
    members = _members(session)
    assert world["tianjin"].id not in members and twin.id in members
    assert summary.moved == 1


# ---- 同一文件：恢复 main 的规则 ---------------------------------------------------


def test_same_url_does_not_merge_different_issuers(session) -> None:
    """恢复 main：网址相同但发文机关不同，仍各自保留。"""
    src = _source(session, "辽宁")
    url = "https://gxt.ln.gov.cn/tzgg/2026091711/index.html"
    title = "辽宁省工业和信息化厅关于2026年度先进制造业企业名单制定工作有关事项的通知"
    old = Document(
        source_id=src.id,
        url=url,
        url_key="gxt.ln.gov.cn/tzgg/2026091711/index.html",  # 旧规则算出的网址键
        title=title,
        publish_date=date(2026, 9, 17),
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    new = Document(
        source_id=src.id,
        url=url,
        url_key="gxt.ln.gov.cn/tzgg/2026091711/",
        title=title,
        publish_date=date(2026, 9, 17),
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    session.add_all([old, new])
    session.flush()
    session.add(
        DocumentDetail(
            document_id=old.id,
            body_text="",
            issuer="辽宁省工业和信息化厅、辽宁省通信管理局",
            fetched_at=NOW,
            status="ok",
        )
    )
    session.commit()
    reconcile_documents(session, {title})
    session.commit()
    assert session.scalars(select(DocumentMerge)).all() == []


# ---- 截止日期不随转发组同步（守护测试） --------------------------------------------


def _confirm_all(session):
    members = _members(session)
    _, fingerprints = member_facts(session, list(members))
    for row in members.values():
        row.confirmed_by, row.confirmed_at = "创始人", NOW
        row.confirmed_fingerprint = fingerprints[row.document_id]
    session.commit()


def test_repost_group_never_syncs_deadlines(session, world) -> None:
    """同组两版有完全相同的候选（类型、日期、时间、证据句都相同），核对一版不影响另一版。"""
    rebuild(session, now=NOW)
    _confirm_all(session)
    a = _deadline(session, world["shaanxi"], candidate_time=time(16, 0))
    b = _deadline(session, world["tianjin"], candidate_time=time(16, 0))
    session.commit()
    shown = admin._pending_shown_ids(session, [a, b])
    assert set(shown) == {a.id, b.id}  # 两版各列一条，不当作“同一文件的副本”只列一条
    a.status, a.reviewed_by, a.approved_date = "approved", "创始人", a.candidate_date
    session.commit()
    sync_reviewed_twins(session, {world["shaanxi"].id, world["tianjin"].id})
    reconcile_documents(session)
    session.commit()
    session.refresh(b)
    assert b.status == "pending" and b.approved_date is None


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


def test_review_api_does_not_copy_result_to_other_versions(client, session, world) -> None:
    rebuild(session, now=NOW)
    session.commit()
    a = _deadline(session, world["shaanxi"])
    b = _deadline(session, world["tianjin"])
    c = _deadline(session, world["fujian"], day=29, evidence="请于9月29日前报送")
    session.commit()
    body = {"reviewed_by": "创始人"}
    assert client.post(f"/api/admin/deadlines/{a.id}/approve", headers=HEADERS, json=body).json()
    for row in (b, c):
        session.refresh(row)
        assert row.status == "pending"
    # 待核对列表：另外两版仍各自列出，带同组对照和“已核对过同一日期”的提示
    items = client.get("/api/admin/deadlines?order=group&due=all", headers=HEADERS).json()
    by_id = {item["id"]: item for item in items}
    assert set(by_id) == {b.id, c.id}
    tianjin = by_id[b.id]
    assert tianjin["repost_label"] == "天津版"
    assert tianjin["repost_same"] == "同组陕西版已核对过同一日期"
    siblings = {s["label"]: s for s in tianjin["repost_siblings"]}
    assert set(siblings) == {"陕西版", "福建版"}
    assert siblings["陕西版"]["deadlines"] == [
        {"kind": "推荐或审核截止", "date": "2026-09-30", "time": None, "status": "approved"}
    ]
    # 福建版的日期不同：不提示，更不会被通过
    assert by_id[c.id]["repost_same"] is None


def test_review_order_group_keeps_versions_together(client, session, world) -> None:
    rebuild(session, now=NOW)
    session.commit()
    rows = [
        _deadline(session, world["shaanxi"], day=12, evidence="12日"),
        _deadline(session, world["other"], day=13, evidence="13日"),
        _deadline(session, world["fujian"], day=28, evidence="28日"),
    ]
    session.commit()
    plain = client.get("/api/admin/deadlines?order=priority&due=all", headers=HEADERS).json()
    assert [item["id"] for item in plain] == [r.id for r in rows]
    grouped = client.get("/api/admin/deadlines?order=group&due=all", headers=HEADERS).json()
    assert [item["id"] for item in grouped] == [rows[0].id, rows[2].id, rows[1].id]
    # 翻页游标：按组排的游标接着用，和按核对顺序的游标不混用
    first = client.get("/api/admin/deadlines?order=group&due=all&limit=1", headers=HEADERS)
    cursor = first.json()[0]["order_key"]
    rest = client.get(
        "/api/admin/deadlines",
        params={"order": "group", "due": "all", "after": cursor},
        headers=HEADERS,
    ).json()
    assert [item["id"] for item in rest] == [rows[2].id, rows[1].id]
    mixed = client.get(
        "/api/admin/deadlines",
        params={"order": "priority", "due": "all", "after": cursor},
        headers=HEADERS,
    )
    assert mixed.status_code == 422


# ---- 管理台转发归组页 ------------------------------------------------------------


def _post(client, path, **body):
    return client.post(
        f"/api/admin/repost-groups{path}", headers=HEADERS, json={"reviewed_by": "创始人", **body}
    )


def _seen(client, gid):
    """管理台页面上看到的这一组（修订号、在组版本）。"""
    for state in ("pending", "confirmed", "removed"):
        for group in client.get(
            "/api/admin/repost-groups", params={"state": state}, headers=HEADERS
        ).json():
            if group["id"] == gid:
                return group
    raise AssertionError(f"转发组 #{gid} 不在管理台列表里")


def _rev(client, gid) -> dict:
    return {"revision": _seen(client, gid)["revision"]}


def _shown(client, gid) -> dict:
    group = _seen(client, gid)
    ids = [m["document_id"] for m in group["members"] if m["active"] and not m["merged_into"]]
    return {"revision": group["revision"], "document_ids": ids}


def test_repost_admin_requires_token(client) -> None:
    assert client.get("/api/admin/repost-groups").status_code == 401
    assert client.post("/api/admin/repost-groups/1/confirm", json={}).status_code == 401
    page = client.get("/admin/repost?embed=1")
    assert page.status_code == 200 and "youjuAdminToken()" in page.text


def test_repost_admin_flow(client, session, world) -> None:
    rebuild(session, now=NOW)
    session.commit()
    _deadline(session, world["tianjin"], candidate_time=time(17, 0))
    session.commit()
    assert client.get("/api/admin/repost-groups/count", headers=HEADERS).json() == {"pending": 1}
    groups = client.get("/api/admin/repost-groups", headers=HEADERS).json()
    assert len(groups) == 1
    group = groups[0]
    assert group["strength_label"] == "强证据" and group["public"] is False
    assert [m["label"] for m in group["members"]] == ["陕西版", "天津版", "福建版"]
    tianjin = group["members"][1]
    assert (
        tianjin["deadlines"][0]["time"] == "17:00"
        and tianjin["deadlines"][0]["status"] == "pending"
    )
    gid = group["id"]

    # 确认整组：对外合并；再确认一次是 409（修订号已变）
    seen = _shown(client, gid)
    assert _post(client, f"/{gid}/confirm", **seen).status_code == 200
    assert _post(client, f"/{gid}/confirm", **seen).status_code == 409
    assert client.get("/api/admin/repost-groups/count", headers=HEADERS).json() == {"pending": 0}
    confirmed = client.get("/api/admin/repost-groups?state=confirmed", headers=HEADERS).json()
    assert confirmed[0]["public"] is True
    # 确认合并不会通过任何截止
    assert session.scalars(select(DocumentDeadline)).one().status == "pending"

    # 撤回：全部回到待确认，保留人工决定
    assert _post(client, f"/{gid}/withdraw", revision=0).status_code == 409  # 修订号对不上
    assert _post(client, f"/{gid}/withdraw", **_rev(client, gid)).status_code == 200
    session.expire_all()
    assert all(row.confirmed_at is None for row in _members(session).values())
    assert session.get(RepostGroup, gid).no_auto is True

    # 移出福建版，再恢复
    fujian = world["fujian"].id
    assert _post(client, f"/{gid}/members/{fujian}/remove").status_code == 200
    assert _post(client, f"/{gid}/members/{fujian}/remove").status_code == 409
    removed = client.get("/api/admin/repost-groups?state=removed", headers=HEADERS).json()
    assert [m["active"] for m in removed[0]["members"]] == [True, True, False]
    assert _post(client, f"/{gid}/members/{fujian}/restore").status_code == 200

    # 拆组：福建版拆成新组；原组至少留一个版本
    assert (
        _post(client, f"/{gid}/split", document_ids=[999], **_rev(client, gid)).status_code == 422
    )
    split = _post(client, f"/{gid}/split", document_ids=[fujian], **_rev(client, gid))
    assert split.status_code == 200
    new_gid = split.json()["new_group_id"]
    session.expire_all()
    assert _members(session)[fujian].group_id == new_gid
    assert (
        _post(
            client,
            f"/{gid}/split",
            document_ids=[world["shaanxi"].id, world["tianjin"].id],
            **_rev(client, gid),
        ).status_code
        == 422
    )

    # 手动加入：福建版已在新组里，先移出才能加回原组
    assert _post(client, f"/{gid}/members", document_id=fujian).status_code == 409
    # 新拆出的组只有一个版本，不在列表里；修订号从 0 开始
    assert _post(client, f"/{new_gid}/reject", revision=0).status_code == 200
    assert _post(client, f"/{gid}/members", document_id=fujian).status_code == 200
    session.expire_all()
    row = _members(session)[fujian]
    assert (row.group_id, row.active, row.joined_by) == (gid, True, "human")
    assert _post(client, "/999/confirm", revision=0, document_ids=[1, 2]).status_code == 404

    # 整组不合并：全部移出，重跑也不会再自动归回
    assert _post(client, f"/{gid}/reject", **_rev(client, gid)).status_code == 200
    rebuild(session, now=NOW)
    session.commit()
    session.expire_all()
    assert not any(row.active for row in _members(session).values())


# ---- Codex 审阅后补的（#183）------------------------------------------------------


@pytest.mark.parametrize(
    ("old_number", "old_body", "new_number", "new_body"),
    [
        ("辽工信函〔2026〕1号", "", "辽工信函〔2026〕2号", ""),  # 文号冲突
        (None, "旧版正文：请于9月30日前报送。" * 3, None, "新版正文：请于10月30日前报送。" * 3),
    ],
)
def test_same_url_with_conflicting_content_is_not_merged(
    session, old_number, old_body, new_number, new_body
) -> None:
    """网址被复用、或改版前后内容不同：文号或正文指纹冲突时两条各自保留，不自动归并，
    免得旧版核定的截止进了新版的详情、导出、日历和雷达。"""
    src = _source(session, "辽宁")
    url = "https://gxt.ln.gov.cn/tzgg/reused/index.html"
    docs = []
    for key, number, body in (
        ("gxt.ln.gov.cn/tzgg/reused/index.html", old_number, old_body),
        ("gxt.ln.gov.cn/tzgg/reused/", new_number, new_body),
    ):
        doc = Document(
            source_id=src.id,
            url=url,
            url_key=key,
            title="辽宁省工业和信息化厅关于组织申报的通知",
            publish_date=date(2026, 9, 17),
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
        session.add(doc)
        session.flush()
        session.add(
            DocumentDetail(
                document_id=doc.id,
                body_text=body,
                doc_number=number,
                fetched_at=NOW,
                status="ok",
            )
        )
        docs.append(doc)
    session.commit()
    reconcile_documents(session)
    session.commit()
    assert session.scalars(select(DocumentMerge)).all() == []


def test_confirm_only_what_the_admin_saw(client, session, world) -> None:
    """确认带着页面上看到的版本和修订号：期间归组任务加了新版本，就不确认（409），刷新后再确认。"""
    rebuild(session, now=NOW)
    session.commit()
    gid = session.scalars(select(RepostGroup.id)).one()
    seen = _shown(client, gid)
    # 看页面的时候，归组任务加进来一个新版本
    _doc(
        session,
        _source(session, "北京"),
        "北京市经济和信息化局关于征集2026年度视听系统典型案例的通知",
        day=11,
    )
    session.commit()
    rebuild(session, now=NOW)
    session.commit()
    stale = _post(client, f"/{gid}/confirm", **seen)
    assert stale.status_code == 409 and "刷新" in stale.json()["detail"]
    # 修订号对上了、版本清单少了一个也不行
    fresh = _shown(client, gid)
    partial = {"revision": fresh["revision"], "document_ids": seen["document_ids"]}
    assert _post(client, f"/{gid}/confirm", **partial).status_code == 409
    session.expire_all()
    assert all(row.confirmed_at is None for row in _members(session).values())
    assert _post(client, f"/{gid}/confirm", **fresh).status_code == 200
    session.expire_all()
    assert all(row.confirmed_at is not None for row in _members(session).values())


def test_duplicate_member_conflict_keeps_human_decision(client, session, world) -> None:
    """组员文件后来成了同一文件的副本、主文件也有组员记录：带人工决定的副本记录不删、不挪，
    在管理台标出来，处理完才能确认；不带决定的副本记录直接删掉。"""
    rebuild(session, now=NOW)
    session.commit()
    members = _members(session)
    gid = members[world["shaanxi"].id].group_id
    # 福建版被人工移出；后来福建版被认定是陕西版的同一文件（另一个网址）
    members[world["fujian"].id].active = False
    members[world["fujian"].id].removed_by = "创始人"
    session.add(
        DocumentMerge(
            duplicate_id=world["fujian"].id,
            canonical_id=world["shaanxi"].id,
            active=True,
            match_key="t",
        )
    )
    session.commit()
    summary = rebuild(session, now=NOW)
    session.commit()
    members = _members(session)
    assert summary.conflicts == 1
    assert members[world["fujian"].id].active is False  # 移出的决定还在
    assert members[world["shaanxi"].id].active is True
    group = _seen(client, gid)
    assert group["conflicts"] == 1
    fujian = next(m for m in group["members"] if m["document_id"] == world["fujian"].id)
    assert fujian["merged_into"] == world["shaanxi"].id and fujian["conflict"] is True
    assert client.get("/api/admin/repost-groups/count", headers=HEADERS).json() == {"pending": 1}
    # 先处理冲突才能确认
    assert _post(client, f"/{gid}/confirm", **_shown(client, gid)).status_code == 409
    # 主文件编号不是副本：不能用“去掉副本记录”
    shaanxi = world["shaanxi"].id
    assert _post(client, f"/{gid}/members/{shaanxi}/drop").status_code == 409
    assert _post(client, f"/{gid}/members/{world['fujian'].id}/drop").status_code == 200
    assert _post(client, f"/{gid}/confirm", **_shown(client, gid)).status_code == 200

    # 不带决定的副本记录：直接删掉，以主文件那条为准
    twin = _doc(session, _source(session, "河北"), TITLE_TWIN, day=9, body=UPSTREAM + "河北")
    session.commit()
    rebuild(session, now=NOW)
    session.commit()
    assert _members(session)[twin.id].confirmed_at is None
    session.add(
        DocumentMerge(
            duplicate_id=twin.id, canonical_id=world["tianjin"].id, active=True, match_key="t"
        )
    )
    session.commit()
    summary = rebuild(session, now=NOW)
    session.commit()
    assert twin.id not in _members(session) and summary.conflicts == 0


TITLE_TWIN = "关于组织开展2026年度视听系统典型案例征集工作的通知"


def test_withdrawn_and_rejected_groups_keep_human_decisions(client, session, world) -> None:
    rebuild(session, now=NOW)
    session.commit()
    gid = session.scalars(select(RepostGroup.id)).one()
    assert _post(client, f"/{gid}/confirm", **_shown(client, gid)).status_code == 200
    assert _post(client, f"/{gid}/withdraw", **_rev(client, gid)).status_code == 200
    # 规则不再支持（标题都改了）：撤回过的组和组员都不被清理
    for key in ("shaanxi", "tianjin", "fujian"):
        world[key].title = f"{world[key].title}（已废止）"
        world[key].kind_override = "名单"
    session.commit()
    summary = rebuild(session, now=NOW)
    session.commit()
    assert summary.dropped == 0
    group = session.get(RepostGroup, gid)
    session.refresh(group)
    assert group.no_auto is True
    members = _members(session)
    assert len(members) == 3 and all(r.active and r.confirmed_at is None for r in members.values())
    # 整组不合并：同样永久保留，重跑也不会再归组
    assert _post(client, f"/{gid}/reject", **_rev(client, gid)).status_code == 200
    for key in ("shaanxi", "tianjin", "fujian"):
        world[key].title = world[key].title.replace("（已废止）", "")
        world[key].kind_override = None
    session.commit()
    summary = rebuild(session, now=NOW)
    session.commit()
    members = _members(session)
    assert len(members) == 3 and not any(r.active for r in members.values())
    assert summary.created == 0
    assert session.get(RepostGroup, gid) is not None


def test_transitive_links_respect_agency_and_date_span() -> None:
    """A、C 是同一机关的不同批次（不能归组），都和 B 匹配也不能连成一组；
    日期跨度超过 90 天的也不能靠中间一份连起来。"""
    title = "关于转发2026年高端医疗装备推广应用项目申报工作的通知"
    a = _cand(1, title, "天津", day=1)
    b = _cand(2, title, "河北", day=5)
    c = _cand(3, title, "天津", day=9)
    groups = propose_groups([a, b, c])
    assert [g.ids for g in groups] == [[1, 2]]  # 天津只能有一份：日期近的先连
    early = Candidate(**{**a.__dict__, "id": 4, "publish_date": date(2026, 6, 20)})
    middle = Candidate(
        **{**b.__dict__, "id": 5, "province": "北京", "publish_date": date(2026, 8, 15)}
    )
    late = Candidate(
        **{**c.__dict__, "id": 6, "province": "山东", "publish_date": date(2026, 9, 28)}
    )
    groups = propose_groups([early, middle, late])
    assert len(groups) == 1 and len(groups[0].ids) == 2
    ids = groups[0].ids
    span = {d.id: d.publish_date for d in (early, middle, late)}
    assert (max(span[i] for i in ids) - min(span[i] for i in ids)).days <= 90


def test_rebuild_does_not_add_same_agency_to_existing_group(session, world) -> None:
    """已有的组里有人工加入的天津版另一批次时，规则不会再把同机关的新版本加进来。"""
    rebuild(session, now=NOW)
    session.commit()
    for row in _members(session).values():
        row.joined_by = "human"
    session.commit()
    gid = session.scalars(select(RepostGroup.id)).one()
    second = _doc(
        session,
        session.get(Source, world["tianjin"].source_id),
        "关于组织开展2026年度视听系统典型案例征集工作的补充通知",
        day=20,
        body=UPSTREAM + "天津补充",
        key="tianjin.example.gov.cn/supplement.html",
    )
    session.commit()
    summary = rebuild(session, now=NOW)
    session.commit()
    members = _members(session)
    assert second.id not in members
    assert summary.joined == 0 and summary.dropped == 0
    # 新来的同机关文件不会把原有的组员“抢走”
    assert {world[k].id for k in ("shaanxi", "tianjin", "fujian")} <= set(members)
    assert all(members[world[k].id].group_id == gid for k in ("shaanxi", "tianjin", "fujian"))
    assert session.get(RepostGroup, gid) is not None


def test_cross_group_add_locks_both_groups_in_order(client, session, world) -> None:
    rebuild(session, now=NOW)
    session.commit()
    gid = session.scalars(select(RepostGroup.id)).one()
    fujian = world["fujian"].id
    split = _post(client, f"/{gid}/split", document_ids=[fujian], **_rev(client, gid))
    new_gid = split.json()["new_group_id"]
    assert _post(client, f"/{new_gid}/reject", revision=0).status_code == 200
    before = session.get(RepostGroup, new_gid).revision
    assert _post(client, f"/{gid}/members", document_id=fujian).status_code == 200
    session.expire_all()
    assert _members(session)[fujian].group_id == gid
    # 两个组的修订号都变了（原来的组少了一条记录）
    assert session.get(RepostGroup, new_gid).revision == before + 1


def test_lock_errors_become_409() -> None:
    from fastapi import HTTPException
    from sqlalchemy.exc import OperationalError

    from youju.api.repost import _busy

    class Fake:
        rolled_back = False

        def rollback(self):
            self.rolled_back = True

    session = Fake()
    with pytest.raises(HTTPException) as caught, _busy(session):
        raise OperationalError("SELECT", {}, Exception(1213, "Deadlock found"))
    assert caught.value.status_code == 409 and session.rolled_back
    with pytest.raises(OperationalError), _busy(Fake()):
        raise OperationalError("SELECT", {}, Exception(2006, "MySQL server has gone away"))


@pytest.mark.parametrize("change", ["agency", "span", "missing_date", "unrelated"])
def test_pending_group_is_recomputed_from_scratch(session, world, change) -> None:
    """旧组员仍有匹配邻居时，也必须整组重算，不能沿用旧的传递连接。"""
    rebuild(session, now=NOW)
    session.commit()
    doc = world["fujian"]
    if change == "agency":
        doc.source_id = world["tianjin"].source_id
    elif change == "span":
        doc.publish_date = date(2026, 12, 15)  # 与天津仍相隔不到 90 天，与陕西相隔超过 90 天
    elif change == "missing_date":
        doc.publish_date = None
    else:
        doc.title = "关于申报另外一个完全不同项目的通知"
        session.get(DocumentDetail, doc.id).body_text = "没有原上游引用"
    session.commit()
    rebuild(session, now=NOW)
    session.commit()
    members = _members(session)
    assert doc.id not in members
    assert len(members) == 2
    revision = session.scalars(select(RepostGroup.revision)).one()
    summary = rebuild(session, now=NOW)
    session.commit()
    assert (summary.created, summary.joined, summary.dropped, summary.moved) == (0, 0, 0, 0)
    assert session.scalars(select(RepostGroup.revision)).one() == revision


def test_pending_groups_can_merge_after_evidence_changes(session, world) -> None:
    rebuild(session, now=NOW)
    other = [
        _doc(
            session,
            _source(session, p),
            "关于转发2026年人工智能应用服务商培育专项行动的通知",
            body="其他上游",
        )
        for p in ("四川", "山东")
    ]
    session.commit()
    rebuild(session, now=NOW)
    session.commit()
    assert len(session.scalars(select(RepostGroup)).all()) == 2
    for doc in other:
        doc.title = world["tianjin"].title
        session.get(DocumentDetail, doc.id).body_text = UPSTREAM
    session.commit()
    rebuild(session, now=NOW)
    session.commit()
    assert len({r.group_id for r in _members(session).values()}) == 1
    assert len(_members(session)) == 5


@pytest.mark.parametrize("change", ["agency", "span", "missing_date"])
def test_invalid_manually_preserved_group_cannot_be_confirmed(
    client, session, world, change
) -> None:
    rebuild(session, now=NOW)
    session.commit()
    gid = session.scalars(select(RepostGroup.id)).one()
    # 人工加入的决定保留，但不能绕过整组约束确认。
    for row in _members(session).values():
        row.joined_by = "human"
    if change == "agency":
        world["fujian"].source_id = world["tianjin"].source_id
    elif change == "span":
        world["fujian"].publish_date = date(2027, 1, 1)
    else:
        world["fujian"].publish_date = None
    session.commit()
    rebuild(session, now=NOW)
    session.commit()
    assert _members(session)[world["fujian"].id].group_id == gid
    response = _post(client, f"/{gid}/confirm", **_shown(client, gid))
    assert response.status_code == 409 and "90 天" in response.json()["detail"]
