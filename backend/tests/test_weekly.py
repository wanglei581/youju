"""周报使用真实测试库：口径、模板、重复生成、口令与迁移回退。"""

import hashlib
import shutil
import subprocess
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient
from lxml import html
from sqlalchemy import inspect, select

from youju import cli, jobs, weekly
from youju.api import app as admin
from youju.config import Settings
from youju.db import get_session
from youju.migrations import alembic_config
from youju.models import (
    DigestIssue,
    DigestProfile,
    Document,
    DocumentDeadline,
    DocumentDetail,
    DocumentField,
    DocumentMerge,
    ListEntry,
    Source,
)

NOW = datetime(2026, 10, 5, 7, 30, tzinfo=weekly.BEIJING)
START = date(2026, 9, 28)
HEADERS = {"X-Admin-Token": "weekly-test-only"}


def source(session, province="浙江", rights=1):
    src = Source(
        level="国家级" if province == "国家" else "省级",
        province=province,
        dept="科技",
        dept_full_name=f"{province}科技部门",
        column_name="通知",
        url=f"https://example.gov.cn/s{province}/{rights}",
        rights_level=rights,
    )
    session.add(src)
    session.flush()
    return src


def document(session, src, title="项目申报通知", published=START, kind="申报通知", url=None):
    n = session.scalar(select(Document.id).order_by(Document.id.desc()).limit(1)) or 0
    doc = Document(
        source_id=src.id,
        title=title,
        url=url or f"https://example.gov.cn/{n + 1}",
        url_key=f"weekly/{n + 1}",
        publish_date=published,
        first_seen_at=NOW,
        last_seen_at=NOW,
        kind_override=kind,
    )
    session.add(doc)
    session.flush()
    return doc


def deadline(session, doc, due=date(2026, 10, 10), status="approved", **kw):
    n = (
        session.scalar(select(DocumentDeadline.id).order_by(DocumentDeadline.id.desc()).limit(1))
        or 0
    )
    row = DocumentDeadline(
        document_id=doc.id,
        fingerprint=f"weekly-{n + 1}",
        kind="企业申报截止",
        candidate_date=due + timedelta(days=1),
        approved_date=due if status != "pending" else None,
        status=status,
        evidence="人工核对证据",
        confidence="高",
        reviewed_by="测试核对人" if status != "pending" else None,
        reviewed_at=NOW if status != "pending" else None,
        **kw,
    )
    session.add(row)
    session.flush()
    return row


def detail(session, doc, body="资金补助正文", **kw):
    row = DocumentDetail(document_id=doc.id, body_text=body, fetched_at=NOW, status="ok", **kw)
    session.add(row)
    session.flush()
    return row


def support(session, doc, value="资金支持", status="approved", body="资金补助正文", **kw):
    row = DocumentField(
        document_id=doc.id,
        field="support",
        value=value,
        approved_value=value,
        status=status,
        evidence="核对证据",
        evidence_offset=0,
        body_hash=hashlib.sha256(body.encode()).hexdigest(),
        reviewed_by="核对人",
        reviewed_at=NOW,
        **kw,
    )
    session.add(row)
    session.flush()
    return row


def entries(session, doc, names=("不可输出企业甲", "不可输出企业乙")):
    for n, name in enumerate(names):
        session.add(
            ListEntry(
                document_id=doc.id,
                source_url=doc.url,
                name=name,
                normalized_name=name,
                sequence=str(n),
                category="企业",
            )
        )
    session.flush()


@pytest.fixture
def profile(session):
    p = DigestProfile(name="全国版")
    session.add(p)
    session.commit()
    return p


@pytest.fixture
def world(session, profile):
    src = source(session)
    national = source(session, "国家")
    private = source(session, "北京", 3)
    restricted = source(session, "北京", 2)
    apply = document(session, src, "项目申报通知")
    twin = document(session, src, "项目申报通知")
    session.add(
        DocumentMerge(duplicate_id=twin.id, canonical_id=apply.id, match_key="weekly", active=True)
    )
    detail(session, twin)
    deadline(session, twin)
    support(session, twin)
    # 政策新发布取详情日期；发现时间全部在本周之外。
    policy = document(session, src, "管理办法", START - timedelta(days=1), "政策")
    detail(session, policy, publish_date=START, doc_number="科〔2026〕1号", issuer="省科技厅")
    listing = document(session, src, "企业名单公示", START, "名单")
    list_twin = document(session, src, "企业名单公示", START, "名单")
    session.add(
        DocumentMerge(
            duplicate_id=list_twin.id, canonical_id=listing.id, match_key="list", active=True
        )
    )
    entries(session, listing)
    entries(session, list_twin)
    bare_list = document(session, src, "企业名单", START, "名单")
    entries(session, bare_list, ("不可输出个人名字",))
    nat = document(session, national, "国家项目申报通知")
    pending = document(session, src, "待核对项目申报通知")
    deadline(session, pending, status="pending")
    detail(session, pending)
    support(session, pending, "贷款贴息", "pending")
    # 同组任何成员下架，主副本都排除。
    hidden = document(session, src, "下架项目申报通知")
    hidden_twin = document(session, src, "下架项目申报通知")
    hidden_twin.taken_down_at = NOW
    session.add(
        DocumentMerge(
            duplicate_id=hidden_twin.id, canonical_id=hidden.id, match_key="hidden", active=True
        )
    )
    deadline(session, hidden)
    for psrc in (private, restricted):
        denied = document(session, psrc, f"非公开{psrc.rights_level}申报通知")
        deadline(session, denied)
    # 有效字段不能沿用旧正文核对结果。
    stale = document(session, src, "正文变化项目申报通知")
    detail(session, stale, body="新正文")
    support(session, stale, "资格认定")
    old = document(session, src, "历史项目申报通知", START - timedelta(days=30))
    deadline(session, old, date(2026, 10, 6), approved_time=time(17))
    expired = document(session, src, "已过期项目申报通知", START - timedelta(days=30))
    deadline(session, expired, date(2026, 10, 4))
    far = document(session, src, "远期项目申报通知", START - timedelta(days=30))
    deadline(session, far, date(2026, 11, 5))
    last = document(session, src, "边界项目申报通知", START - timedelta(days=30))
    deadline(session, last, date(2026, 11, 4))
    session.commit()
    return {
        "src": src,
        "apply": apply,
        "twin": twin,
        "listing": listing,
        "bare_list": bare_list,
        "nat": nat,
        "policy": policy,
        "hidden": hidden,
        "pending": pending,
        "old": old,
    }


def generate(session, profile, week=START):
    return weekly.generate(session, profile.id, week, NOW)


def test_four_columns_public_reviewed_dedup_no_names(session, profile, world):
    result = generate(session, profile)
    issue = result.issue
    assert result.outcome == "generated"
    assert issue.counts == {"applications": 4, "deadlines": 3, "lists": 2, "policies": 1}
    assert issue.title == "政策申报周报｜9 月 28 日—10 月 4 日（全国版）"
    text = issue.body_text
    assert text.splitlines().count("项目申报通知") == 2  # 新发布栏目及未来截止栏目各一条
    assert "国家部委（1 条）" in text and text.index("国家部委（1 条）") < text.index(
        "浙江（3 条）"
    )
    assert "9 月 28 日发布" in text and "截止：10 月 10 日（已核对）" in text
    assert "10 月 11 日" not in text  # 候选日期不能输出
    assert "截止：10 月 6 日 17:00（已核对）" in text and "截止：11 月 4 日（已核对）" in text
    assert "支持方式：资金支持" in text
    assert "贷款贴息" not in text and "资格认定" not in text
    assert "截止时间见原文" in text
    assert "9 月 28 日 · 公示 · 2 条" in text and "9 月 28 日 · 1 条" in text
    assert "不可输出" not in text and "· 4 条" not in text
    tree = html.fragment_fromstring(issue.body_html)
    assert tree.xpath(".//strong[text()='企业名单公示']")
    assert not tree.xpath(".//a")
    assert "http" not in text and "http" not in issue.body_html
    assert world["listing"].url in issue.body_text_with_links
    assert "企业名单\n浙江科技部门 · 9 月 28 日 · 1 条" in text  # 未额外添加公示或公布状态
    assert "科〔2026〕1号" in text
    for word in ("下架项目", "非公开", "已过期", "远期"):
        assert word not in text
    assert text.splitlines()[-2:] == [weekly.CALL_TO_ACTION, weekly.DISCLAIMER]
    # 每条恰好两个段落，标题加粗，下一段小字；纯文本与 HTML 的行一一对应。
    assert "\n".join(tree.xpath("./*/text() | ./p/strong/text()")) == "\n".join(
        line for line in text.splitlines() if line
    )
    titles = tree.xpath("./p[strong]")
    assert len(titles) == sum(issue.counts.values())
    assert all(
        p.getnext().tag == "p" and "font-size:12px" in p.getnext().get("style") for p in titles
    )
    for line in issue.body_text_with_links.splitlines():
        if "｜" in line:
            assert len(line.split("｜")) == 5 and line.split("｜")[-1].startswith("https://")


def test_changed_twin_suppresses_old_approved_deadline(session, profile, world):
    changed = deadline(
        session,
        world["apply"],
        status="changed",
        change_reason="延期",
        changed_to_document_id=world["nat"].id,
    )
    changed.changed_at = NOW
    session.commit()
    issue = generate(session, profile).issue
    assert issue.counts["deadlines"] == 2
    assert "10 月 10 日" not in issue.body_text
    assert "2026-10-10" not in issue.body_text_with_links


def test_fixed_province_order_all_columns_and_group_counts(session, profile):
    # 倒序插入、发布日期和截止日期也故意倒序，避免碰巧按出现顺序通过。
    for n, province in enumerate(("安徽", "浙江", "江苏", "内蒙古", "北京", "国家")):
        src = source(session, province)
        for kind in ("政策", "名单", "申报通知"):
            doc = document(session, src, f"{province}{kind}", START + timedelta(days=n), kind)
            if kind == "申报通知":
                deadline(session, doc, NOW.date() + timedelta(days=n))
    issue = generate(session, profile).issue
    root = html.fragment_fromstring(issue.body_html)
    expected = [
        "国家部委（1 条）",
        "北京（1 条）",
        "内蒙古（1 条）",
        "江苏（1 条）",
        "浙江（1 条）",
        "安徽（1 条）",
    ]
    assert root.xpath("./h3/text()") == expected * 4
    for label in weekly.COLUMNS.values():
        assert f"{label}（6 条）" in issue.body_text
    linked_titles = [
        line.split("｜")[0] for line in issue.body_text_with_links.splitlines() if "｜" in line
    ]
    assert linked_titles[:6] == [
        f"{p}申报通知" for p in ("国家", "北京", "内蒙古", "江苏", "浙江", "安徽")
    ]


def test_compact_lists_omit_unknown_count_and_unstated_status(session, profile):
    src = source(session)
    for title in ("名单", "名单公示", "名单公布", "公示及公布名单"):
        document(session, src, title, kind="名单")
    issue = generate(session, profile).issue
    lines = issue.body_text.splitlines()
    assert lines[lines.index("名单") + 1] == "浙江科技部门 · 9 月 28 日"
    assert lines[lines.index("名单公示") + 1].endswith(" · 公示")
    assert lines[lines.index("名单公布") + 1].endswith(" · 公布")
    assert lines[lines.index("公示及公布名单") + 1].endswith(" · 公示/公布")
    assert "0 条" not in issue.body_text


def test_compact_multiple_deadlines_and_no_embedded_urls(session, profile):
    src = source(session)
    src.dept_full_name = "科技厅 https://example.gov.cn/contact"
    doc = document(session, src, "申报\n通知 https://example.gov.cn/note www.example.gov.cn")
    deadline(session, doc, approved_time=time(17))
    other = deadline(session, doc, date(2026, 10, 12))
    other.kind = "材料报送截止"
    issue = generate(session, profile).issue
    assert (
        "截止：企业申报截止 10 月 10 日 17:00（已核对）；材料报送截止 10 月 12 日（已核对）"
        in issue.body_text
    )
    assert "https://" not in issue.body_text + issue.body_html
    assert "www." not in issue.body_text + issue.body_html
    assert "申报 通知" in issue.body_text.splitlines()
    linked = [line for line in issue.body_text_with_links.splitlines() if "｜" in line]
    assert len(linked) == 2
    assert "2026-10-10 17:00（已核对）" in linked[0]
    assert "材料报送截止 2026-10-12（已核对）" in linked[0]
    assert linked[0].split("｜")[-1] == doc.url


@pytest.mark.parametrize(
    "stamp, expected",
    [
        (datetime(2026, 10, 4, 15, 59, 59, tzinfo=UTC), (date(2026, 9, 21), date(2026, 9, 27))),
        (datetime(2026, 10, 4, 16, 0, tzinfo=UTC), (START, date(2026, 10, 4))),
        (NOW, (START, date(2026, 10, 4))),
    ],
)
def test_beijing_monday_boundary(stamp, expected):
    assert weekly.week_interval(stamp=stamp) == expected


def test_publication_dates_not_first_seen_and_week_edges(session, profile):
    src = source(session)
    for day, title in [
        (date(2026, 9, 27), "前周"),
        (START, "周一"),
        (date(2026, 10, 4), "周日"),
        (date(2026, 10, 5), "下周"),
    ]:
        document(session, src, title, day, "政策")
    issue = generate(session, profile).issue
    assert issue.counts["policies"] == 2
    assert "前周" not in issue.body_text and "下周" not in issue.body_text
    assert "文号：见原文" in issue.body_text and "原文未提供" not in issue.body_text
    with pytest.raises(ValueError, match="周一"):
        generate(session, profile, date(2026, 9, 29))


def test_provinces_keywords_column_switches(session, profile, world):
    profile.provinces = ["北京"]
    session.commit()
    issue = generate(session, profile).issue
    assert issue.counts["applications"] == 1 and "国家项目" in issue.body_text
    assert "浙江科技" not in issue.body_text
    profile.provinces = ["浙江"]
    profile.keywords = ["国家项目", "待核对"]
    profile.columns = {**profile.columns, "deadlines": False, "lists": False, "policies": False}
    session.commit()
    issue = generate(session, profile).issue
    assert issue.counts == {"applications": 2, "deadlines": 0, "lists": 0, "policies": 0}
    assert "未来 30 天内截止" not in issue.body_text
    assert "本周公示/公布的名单" not in issue.body_text
    profile.keywords = ["项目%"]  # 通配符按字面匹配
    session.commit()
    assert generate(session, profile).outcome == "empty"


@pytest.mark.parametrize(
    "column, kind",
    [
        ("applications", "申报通知"),
        ("deadlines", "申报通知"),
        ("lists", "名单"),
        ("policies", "政策"),
    ],
)
def test_column_cap_has_total_count(session, profile, column, kind):
    src = source(session)
    profile.columns = dict.fromkeys(weekly.COLUMNS, False) | {column: True}
    for n in range(35):
        doc = document(session, src, f"题录 {n:02}", START, kind)
        if column == "deadlines":
            deadline(session, doc, NOW.date() + timedelta(days=n % 30))
    session.commit()
    issue = generate(session, profile).issue
    assert issue.counts[column] == 35
    assert issue.body_text.count("题录 ") == 30
    assert "共 35 条，其余在小程序里查看" in issue.body_text


def test_empty_no_issue_with_reason_and_old_draft_removed(session, profile, caplog):
    with caplog.at_level("INFO", logger="youju.weekly"):
        assert generate(session, profile).issue is None
    assert "周报未生成" in caplog.text
    src = source(session)
    doc = document(session, src)
    session.commit()
    assert generate(session, profile).issue is not None
    doc.taken_down_at = NOW
    session.commit()
    assert generate(session, profile).outcome == "empty"
    assert session.scalars(select(DigestIssue)).all() == []


def test_draft_replace_published_protected_other_profile_separate(session, profile, world):
    issue = generate(session, profile).issue
    issue_id, body = issue.id, issue.body_text
    world["apply"].title = "改后的项目申报通知"
    session.commit()
    assert generate(session, profile).issue.id == issue_id
    assert issue.body_text != body
    issue.status = "published"
    issue.note = "公众号已发布"
    session.commit()
    frozen = issue.body_text, issue.body_text_with_links, issue.generated_at
    world["apply"].title = "再次修改"
    session.commit()
    assert generate(session, profile).outcome == "published"
    assert (issue.body_text, issue.body_text_with_links, issue.generated_at) == frozen
    assert issue.note == "公众号已发布"
    other = DigestProfile(name="地区版")
    session.add(other)
    session.commit()
    assert generate(session, other).issue.id != issue_id


def test_html_allowlist_injection_and_forbidden_terms(session, profile):
    src = source(session)
    for title in (
        '题录<script>alert(1)</script><img src="https://example.org/a">',
        '题录 class="abc" & < >',
    ):
        document(session, src, title, kind="政策")
    for title in ("智能项目", "AI 项目", "重磅政策", "必看名单", "最佳方案"):
        document(session, src, title, kind="政策")
    document(session, src, "危险链接", kind="政策", url="javascript:alert(1)")
    session.commit()
    issue = generate(session, profile).issue
    root = html.fragment_fromstring(issue.body_html)
    # 政府原标题照录：含"智能""最"等字的通知不能被排除（否则智能制造类通知全漏）
    assert issue.counts["policies"] == 7
    for element in root.iter():
        assert element.tag in {"section", "p", "h2", "h3", "strong"}
        assert set(element.attrib) <= {"style"}
    assert not root.xpath(".//script | .//img | .//a | .//*[@class]")
    assert "https://" not in issue.body_html and "https://" not in issue.body_text
    assert "&lt;script&gt;" in issue.body_html and "&amp;" in issue.body_html
    assert "智能项目" in issue.body_text and "AI 项目" in issue.body_text
    # 我们自己的模板文字里不出现这些词：去掉题录标题后再检查
    template_only = issue.body_text
    for title in ("智能项目", "AI 项目", "重磅政策", "必看名单", "最佳方案"):
        template_only = template_only.replace(title, "")
    assert not weekly.FORBIDDEN.search(template_only)


@pytest.fixture
def client(session_factory, monkeypatch):
    settings = Settings(_env_file=None, admin_token=HEADERS["X-Admin-Token"], role="all")
    monkeypatch.setattr(admin, "get_settings", lambda: settings)
    monkeypatch.setattr(weekly, "now", lambda: NOW)

    def override():
        with session_factory() as s:
            yield s

    admin.app.dependency_overrides[get_session] = override
    with TestClient(admin.app) as c:
        yield c
    admin.app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/profiles", None),
        ("post", "/profiles", {"name": "测试"}),
        ("put", "/profiles/1", {"name": "测试"}),
        ("get", "/issues", None),
        ("get", "/issues/1", None),
        ("post", "/generate", {"profile_id": 1}),
        ("post", "/issues/1/regenerate", None),
        ("post", "/issues/1/status", {"status": "published"}),
    ],
)
def test_all_endpoints_require_token(client, method, path, body):
    kwargs = {"json": body} if body is not None else {}
    assert getattr(client, method)(f"/api/admin/weekly{path}", **kwargs).status_code == 401


def test_api_profiles_generate_publish_retract_audit(client, session, profile, world, monkeypatch):
    seen = []
    monkeypatch.setattr(admin.audit_log, "info", lambda value: seen.append(value))
    prefix = "/api/admin/weekly"
    config = {
        "name": "浙江版",
        "provinces": ["浙江"],
        "keywords": ["项目"],
        "weekday": 3,
        "enabled": False,
    }
    created = client.post(prefix + "/profiles", json=config, headers=HEADERS)
    assert created.status_code == 201
    pid = created.json()["id"]
    assert client.post(prefix + "/profiles", json=config, headers=HEADERS).status_code == 409
    config["name"] = "浙江项目版"
    assert client.put(f"{prefix}/profiles/{pid}", json=config, headers=HEADERS).status_code == 200
    assert len(client.get(prefix + "/profiles", headers=HEADERS).json()["items"]) == 2
    for patch in (
        {"provinces": ["浙江省"]},
        {"weekday": 0},
        {"name": "智能版"},
        {"columns": {"bad": True}},
    ):
        assert (
            client.put(f"{prefix}/profiles/{pid}", json=config | patch, headers=HEADERS).status_code
            == 422
        )
    assert (
        client.post(
            prefix + "/generate",
            json={"profile_id": profile.id, "week": "2026-09-29"},
            headers=HEADERS,
        ).status_code
        == 422
    )
    response = client.post(
        prefix + "/generate",
        json={"profile_id": profile.id, "week": START.isoformat()},
        headers=HEADERS,
    )
    assert response.status_code == 200 and response.json()["outcome"] == "generated"
    iid = response.json()["issue_id"]
    summary = client.get(prefix + "/issues", headers=HEADERS).json()
    assert summary["total"] == 1 and "body_html" not in summary["items"][0]
    assert "body_text_with_links" not in summary["items"][0]
    detail_response = client.get(f"{prefix}/issues/{iid}", headers=HEADERS).json()
    assert "https://example.gov.cn/" in detail_response["body_text_with_links"]
    assert "https://" not in detail_response["body_text"]
    from youju.sources.onboard import PROVINCES

    assert client.get(prefix + "/profiles", headers=HEADERS).json()["provinces"] == list(
        PROVINCES[1:]
    )
    assert client.get(prefix + "/issues", headers=HEADERS).headers["cache-control"] == "no-store"
    assert (
        client.get(f"{prefix}/issues/{iid}", headers=HEADERS)
        .json()["body_html"]
        .startswith("<section")
    )
    assert (
        client.post(
            f"{prefix}/issues/{iid}/status",
            json={"status": "published", "note": "已发布"},
            headers=HEADERS,
        ).json()["status"]
        == "published"
    )
    assert (
        client.post(f"{prefix}/issues/{iid}/regenerate", headers=HEADERS).json()["outcome"]
        == "published"
    )
    assert (
        client.post(
            f"{prefix}/issues/{iid}/status", json={"status": "draft"}, headers=HEADERS
        ).json()["status"]
        == "draft"
    )
    assert (
        client.post(f"{prefix}/issues/{iid}/regenerate", headers=HEADERS).json()["outcome"]
        == "generated"
    )
    for event in (
        "weekly_profile_create",
        "weekly_profile_update",
        "weekly_profiles_list",
        "weekly_generate",
        "weekly_issues_list",
        "weekly_issue_view",
        "weekly_status",
    ):
        assert any(event in record for record in seen)
    assert all(HEADERS["X-Admin-Token"] not in record for record in seen)


def test_snapshot_down_after_generation_blocks_view_publish_allows_retraction(
    client, session, profile, world
):
    issue = generate(session, profile).issue
    issue.status = "published"
    session.commit()
    world["twin"].taken_down_at = NOW
    session.commit()
    path = f"/api/admin/weekly/issues/{issue.id}"
    assert client.get(path, headers=HEADERS).status_code == 409
    assert (
        client.post(path + "/status", json={"status": "published"}, headers=HEADERS).status_code
        == 409
    )
    assert client.post(path + "/regenerate", headers=HEADERS).json()["outcome"] == "published"
    assert (
        client.post(path + "/status", json={"status": "draft"}, headers=HEADERS).status_code == 200
    )
    assert client.post(path + "/regenerate", headers=HEADERS).json()["outcome"] == "generated"
    assert "项目申报通知" not in client.get(path, headers=HEADERS).json()["body_text"].splitlines()


def test_api_empty_missing_pagination_and_page(client, session, profile, world):
    prefix = "/api/admin/weekly"
    assert client.get(prefix + "/issues/999", headers=HEADERS).status_code == 404
    assert client.post(prefix + "/issues/999/regenerate", headers=HEADERS).status_code == 404
    assert (
        client.put(prefix + "/profiles/999", json={"name": "缺失"}, headers=HEADERS).status_code
        == 404
    )
    profile.columns = dict.fromkeys(weekly.COLUMNS, False)
    session.commit()
    assert (
        client.post(prefix + "/generate", json={"profile_id": profile.id}, headers=HEADERS).json()[
            "outcome"
        ]
        == "empty"
    )
    assert client.get(prefix + "/issues", headers=HEADERS).json()["total"] == 0
    page = client.get("/admin/weekly?embed=1")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert "/admin/embed.js" in page.text and "youjuAdminToken()" in page.text
    assert "'text/html'" in page.text and "manualCopy" in page.text
    assert 'data-menu="weekly"' in client.get("/admin").text
    assert 'allow="clipboard-write"' in client.get("/admin").text


def test_cli_week_profiles_and_schedule(
    session_factory, session, profile, world, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    monkeypatch.setattr(weekly, "now", lambda: NOW)
    assert cli.main(["weekly-digest", "--profile", "全国版", "--week", "2026-09-28"]) == 0
    assert "草稿已生成" in capsys.readouterr().out
    assert "weekly-digest" in jobs.OVERVIEW_JOBS
    profile.enabled = False
    session.commit()
    assert cli.main(["weekly-digest", "--scheduled"]) == 0
    assert "没有需要生成" in capsys.readouterr().out
    assert cli.main(["weekly-digest", "--profile", "不存在"]) == 1
    assert cli.main(["weekly-digest", "--week", "2026-09-29"]) == 1
    profile.enabled = True
    profile.weekday = 2
    session.commit()
    assert cli.main(["weekly-digest", "--scheduled"]) == 0
    assert "没有需要生成" in capsys.readouterr().out
    monkeypatch.setattr(weekly, "now", lambda: NOW + timedelta(days=1))
    assert cli.main(["weekly-digest", "--scheduled"]) == 0
    assert "草稿已生成" in capsys.readouterr().out


def test_migration_0013_upgrade_downgrade(db_engine, session):
    # conftest 的测试库 URL；禁止这个测试对开发库执行。
    assert db_engine.url.database.startswith("youju_test")
    src = source(session)
    doc = document(session, src)
    session.commit()
    cfg = alembic_config(db_engine.url.render_as_string(hide_password=False))
    try:
        command.downgrade(cfg, "0012")
        assert "digest_profiles" not in inspect(db_engine).get_table_names()
        assert "digest_issues" not in inspect(db_engine).get_table_names()
    finally:
        command.upgrade(cfg, "head")
    session.expire_all()
    seed = session.scalar(select(DigestProfile))
    assert seed.name == "全国版" and seed.provinces == [] and seed.weekday == 1 and seed.enabled
    assert all(seed.columns.values())
    assert session.get(Document, doc.id).title == "项目申报通知"
    assert session.scalars(select(DigestIssue)).all() == []
    columns = {c["name"]: c for c in inspect(db_engine).get_columns("digest_issues")}
    assert not columns["body_text_with_links"]["nullable"]


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_weekly_javascript_syntax(tmp_path):
    from lxml import html

    page = Path(__file__).resolve().parents[1] / "src/youju/admin/weekly.html"
    scripts = html.fromstring(page.read_text()).xpath("//script[not(@src)]/text()")
    file = tmp_path / "weekly.js"
    file.write_text("\n".join(scripts))
    result = subprocess.run(
        ["node", "--check", str(file)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_deadline_today_respects_reviewed_time(session, profile):
    src = source(session)
    for title, at in (("早已截止", time(7)), ("今天仍可申报", time(8)), ("当天未写时刻", None)):
        doc = document(session, src, title, None)
        deadline(session, doc, NOW.date(), approved_time=at)
    session.commit()
    issue = generate(session, profile).issue
    assert issue.counts["deadlines"] == 2
    assert (
        "早已截止" not in issue.body_text and "截止：10 月 5 日 08:00（已核对）" in issue.body_text
    )
    assert "发布日期见原文" in issue.body_text


def test_concurrent_generation_one_issue_and_publish_keeps_snapshot(
    session, session_factory, profile, world
):
    from concurrent.futures import ThreadPoolExecutor

    def run():
        with session_factory() as worker:
            return weekly.generate(worker, profile.id, START, NOW).issue.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: run(), range(2)))
    assert ids[0] == ids[1]
    assert len(session.scalars(select(DigestIssue)).all()) == 1
    issue = session.get(DigestIssue, ids[0])
    issue.status = "published"
    session.commit()
    snapshot = issue.body_text
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: run(), range(2))) == ids
    session.refresh(issue)
    assert issue.body_text == snapshot and issue.status == "published"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_weekly_clipboard_behavior():
    script = Path(__file__).resolve().parent / "js/weekly.cjs"
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


# ---- 临期提醒（每周四）：口径与小程序截止雷达一致 ----

THU = datetime(2026, 10, 8, 7, 30, tzinfo=weekly.BEIJING)
THIS_WEEK = date(2026, 10, 5)
REMINDER_ORDER = [
    "今天下午截止",
    "国家三天后截止",
    "三天后截止",
    "归并主文件截止",
    "只有材料报送截止",
    "第七天截止",
]


def reminder(session, profile, stamp=THU, week=None, **kw):
    return weekly.generate(session, profile.id, week, stamp, "reminder", **kw)


def linked_rows(issue):
    return [line.split("｜") for line in issue.body_text_with_links.splitlines() if "｜" in line]


@pytest.fixture
def due_world(session, profile):
    src = source(session)
    national = source(session, "国家")
    private = source(session, "北京", 3)
    day = THU.date()
    docs = {}

    def notice(title, due, where=src, kind="申报通知", **kw):
        doc = document(session, where, title, START, kind)
        if due is not None:
            deadline(session, doc, due, **kw)
        docs[title] = doc
        return doc

    notice("三天后截止", day + timedelta(days=3))
    notice("今天下午截止", day, approved_time=time(17))
    notice("今天早上已截止", day, approved_time=time(7))
    notice("第七天截止", day + timedelta(days=7))
    notice("第八天截止", day + timedelta(days=8))
    notice("昨天已截止", day - timedelta(days=1))
    notice("待核对截止", day + timedelta(days=2), status="pending")
    notice("已驳回截止", day + timedelta(days=2), status="rejected")
    notice("国家三天后截止", day + timedelta(days=3), national, approved_time=time(9))
    notice("非公开截止", day + timedelta(days=2), private)
    notice("政策文件截止", day + timedelta(days=2), kind="政策")
    notice("已下架截止", day + timedelta(days=2)).taken_down_at = THU
    hidden = notice("副本下架截止", day + timedelta(days=2))
    hidden_twin = document(session, src, "副本下架截止", START)
    hidden_twin.taken_down_at = THU
    session.add(
        DocumentMerge(
            duplicate_id=hidden_twin.id, canonical_id=hidden.id, match_key="r-hidden", active=True
        )
    )
    # 核对过的截止在副本上：主文件只列一次，发文单位取同组详情。
    master = notice("归并主文件截止", None)
    twin = document(session, src, "归并主文件截止", START)
    session.add(
        DocumentMerge(duplicate_id=twin.id, canonical_id=master.id, match_key="r-twin", active=True)
    )
    deadline(session, twin, day + timedelta(days=5))
    detail(session, twin, issuer="省科技厅")
    # 同组已标"已变更"的相同截止不再有效。
    moved = notice("已延期截止", day + timedelta(days=4))
    changed = deadline(
        session,
        moved,
        day + timedelta(days=4),
        status="changed",
        change_reason="延期",
        changed_to_document_id=docs["三天后截止"].id,
    )
    changed.changed_at = THU
    # 主截止优先取企业申报截止（截止雷达口径）：企业申报截止在 7 天外就不收。
    preferred = notice("企业截止在七天外", day + timedelta(days=10))
    deadline(session, preferred, day + timedelta(days=2)).kind = "材料报送截止"
    material = notice("只有材料报送截止", None)
    deadline(session, material, day + timedelta(days=6)).kind = "材料报送截止"
    session.commit()
    return docs


def test_reminder_reviewed_within_seven_days_sorted_like_mini_program(
    session, profile, due_world, monkeypatch
):
    result = reminder(session, profile)
    issue = result.issue
    assert result.outcome == "generated"
    assert (issue.kind, issue.week_start, issue.week_end) == (
        "reminder",
        THIS_WEEK,
        date(2026, 10, 11),
    )
    assert issue.title == "申报临期提醒｜10 月 8 日—10 月 15 日（全国版）"
    assert issue.counts == {"reminder": 6}
    assert [row[0] for row in linked_rows(issue)] == REMINDER_ORDER
    assert issue.document_ids == sorted(due_world[title].id for title in REMINDER_ORDER)
    text = issue.body_text
    for word in (
        "早上",
        "第八天",
        "昨天",
        "待核对",
        "已驳回",
        "非公开",
        "政策文件",
        "下架",
        "已延期",
    ):
        assert word not in text + issue.body_text_with_links
    assert "企业截止在七天外" not in text
    lines = text.splitlines()
    assert lines[:2] == ["未来 7 天内截止的申报通知（6 条）", "截止统计基准：2026-10-08"]
    assert lines[lines.index("今天下午截止") + 1] == (
        "浙江科技部门 · 截止：企业申报截止 10 月 8 日 17:00（已核对）"
    )
    assert lines[lines.index("归并主文件截止") + 1] == (
        "省科技厅 · 截止：企业申报截止 10 月 13 日（已核对）"
    )
    assert lines[lines.index("只有材料报送截止") + 1] == (
        "浙江科技部门 · 截止：材料报送截止 10 月 14 日（已核对）"
    )
    assert "10 月 16 日" not in text and "10 月 9 日" not in text  # 8 天后与候选日期都不输出
    # 和小程序截止雷达（未来 7 天）逐条一致、顺序一致。
    from youju.api import v1

    monkeypatch.setattr(v1, "now", lambda: THU)
    radar = v1._report_payload(session, 7, "", "", "", 1, 100)["items"]
    assert [item["title"] for item in radar] == REMINDER_ORDER


def test_reminder_two_versions_format_and_forbidden_terms(session, profile, due_world):
    issue = reminder(session, profile).issue
    tree = html.fragment_fromstring(issue.body_html)
    for element in tree.iter():
        assert element.tag in {"section", "p", "h2", "strong"}
        assert set(element.attrib) <= {"style"}
    assert not tree.xpath(".//a | .//h3")
    assert "http" not in issue.body_text and "http" not in issue.body_html
    # 公众号版：每条加粗标题加一行小字；纯文本与 HTML 的行一一对应。
    assert "\n".join(tree.xpath("./*/text() | ./p/strong/text()")) == "\n".join(
        line for line in issue.body_text.splitlines() if line
    )
    titles = tree.xpath("./p[strong]")
    assert [p.findtext("strong") for p in titles] == REMINDER_ORDER
    assert all("font-size:12px" in p.getnext().get("style") for p in titles)
    assert issue.body_text.splitlines()[-2:] == [weekly.CALL_TO_ACTION, weekly.DISCLAIMER]
    # 带链接版：每条一行四列，写完整日期与截止类型。
    rows = linked_rows(issue)
    assert all(len(row) == 4 and row[-1].startswith("https://example.gov.cn/") for row in rows)
    assert rows[0] == [
        "今天下午截止",
        "浙江科技部门",
        "截止：企业申报截止 2026-10-08 17:00（已核对）",
        due_world["今天下午截止"].url,
    ]
    assert rows[4][2] == "截止：材料报送截止 2026-10-14（已核对）"
    assert issue.body_text_with_links.splitlines()[-2:] == [
        weekly.CALL_TO_ACTION,
        weekly.DISCLAIMER,
    ]
    template_only = issue.title + issue.body_text + issue.body_text_with_links
    for value in (*REMINDER_ORDER, "浙江科技部门", "国家科技部门", "省科技厅"):
        template_only = template_only.replace(value, "")
    assert not weekly.FORBIDDEN.search(template_only)


def test_reminder_empty_still_generates_one_sentence(session, profile, due_world):
    profile.keywords = ["没有这样的标题"]
    session.commit()
    result = reminder(session, profile)
    issue = result.issue
    assert result.outcome == "generated" and issue.counts == {"reminder": 0}
    assert issue.document_ids == []
    expected = [weekly.REMINDER_EMPTY, "", weekly.CALL_TO_ACTION, weekly.DISCLAIMER]
    assert issue.body_text.splitlines() == expected
    assert issue.body_text_with_links.splitlines() == expected
    assert weekly.REMINDER_EMPTY == "本周未来 7 天没有已核对截止的申报项目"
    tree = html.fragment_fromstring(issue.body_html)
    assert tree.xpath("./p/text()") == [r for r in expected if r]
    assert weekly.issue_available(session, issue)


def test_reminder_profile_filters_and_cap(session, profile, due_world):
    profile.provinces = ["北京"]
    session.commit()
    issue = reminder(session, profile).issue
    assert [row[0] for row in linked_rows(issue)] == ["国家三天后截止"]
    profile.provinces = []
    profile.keywords = ["三天后", "第七天"]
    session.commit()
    issue = reminder(session, profile).issue
    assert [row[0] for row in linked_rows(issue)] == ["国家三天后截止", "三天后截止", "第七天截止"]
    profile.keywords = ["题录"]
    src = due_world["三天后截止"].source_id
    for n in range(35):
        doc = document(session, session.get(Source, src), f"题录 {n:02}", START)
        deadline(session, doc, THU.date() + timedelta(days=n % 8))
    session.commit()
    issue = reminder(session, profile).issue
    assert issue.counts == {"reminder": 35} and len(issue.document_ids) == 30
    assert issue.body_text.count("题录 ") == 30
    assert "共 35 条，其余在小程序里查看" in issue.body_text


def test_reminder_separate_from_weekly_draft_replaced_published_kept(session, profile, due_world):
    weekly_issue = weekly.generate(session, profile.id, THIS_WEEK, THU).issue
    issue = reminder(session, profile).issue
    assert weekly_issue.week_start == issue.week_start == THIS_WEEK
    assert (weekly_issue.kind, issue.kind) == ("weekly", "reminder")
    assert weekly_issue.id != issue.id and "临期" not in weekly_issue.title
    # 同一周再生成覆盖草稿，不新增；周一到周日任何一天都归入同一周。
    due_world["三天后截止"].title = "改后的三天后截止"
    session.commit()
    sunday = THU + timedelta(days=3)
    again = reminder(session, profile, sunday, THIS_WEEK).issue
    assert again.id == issue.id and "改后的三天后截止" in again.body_text
    assert again.title == "申报临期提醒｜10 月 11 日—10 月 18 日（全国版）"
    issue.status = "published"
    session.commit()
    frozen = issue.body_text
    due_world["三天后截止"].title = "再次修改"
    session.commit()
    assert reminder(session, profile).outcome == "published"
    assert issue.body_text == frozen
    assert len(session.scalars(select(DigestIssue)).all()) == 2
    # 截止按生成当天计算，不能补生成以前的周。
    with pytest.raises(ValueError, match="只能生成本周"):
        reminder(session, profile, week=START)
    with pytest.raises(ValueError, match="周一"):
        reminder(session, profile, week=THU.date())
    with pytest.raises(ValueError, match="类型"):
        weekly.generate(session, profile.id, None, THU, "monthly")


def test_reminder_only_missing_skips_existing(session, profile, due_world):
    first = reminder(session, profile, only_missing=True)
    assert first.outcome == "generated"
    due_world["三天后截止"].title = "改后的标题"
    session.commit()
    second = reminder(session, profile, THU + timedelta(days=1), only_missing=True)
    assert second.outcome == "exists" and second.issue.id == first.issue.id
    assert "改后的标题" not in second.issue.body_text
    assert len(session.scalars(select(DigestIssue)).all()) == 1


def test_cli_reminder_thursday_schedule_catch_up_no_duplicate(
    session_factory, session, profile, due_world, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    other = DigestProfile(name="停用版", enabled=False)
    session.add(other)
    session.commit()

    def run(stamp, *extra):
        monkeypatch.setattr(weekly, "now", lambda: stamp)
        code = cli.main(["weekly-digest", "--kind", "reminder", *extra])
        return code, capsys.readouterr().out

    def reminders():
        session.expire_all()
        return session.scalars(
            select(DigestIssue).where(DigestIssue.kind == "reminder").order_by(DigestIssue.id)
        ).all()

    # 周三不安排；周四为启用方案生成，与方案的周报生成星期无关。
    assert run(THU - timedelta(days=1), "--scheduled") == (
        0,
        "临期提醒每周四生成、周五到周日补跑，今天不安排\n",
    )
    assert reminders() == []
    code, out = run(THU, "--scheduled")
    assert code == 0 and "草稿已生成（临期提醒 #" in out
    [issue] = reminders()
    assert (issue.profile_id, issue.week_start) == (profile.id, THIS_WEEK)
    snapshot = issue.body_text
    due_world["三天后截止"].title = "周五改过的标题"
    session.commit()
    # 同一天重跑和周五到周日补跑都不重复生成、不覆盖。
    for stamp in (THU, THU + timedelta(days=1), THU + timedelta(days=3)):
        code, out = run(stamp, "--scheduled")
        assert code == 0 and "本周临期提醒已生成，未重复生成" in out
    assert [(row.id, row.body_text) for row in reminders()] == [(issue.id, snapshot)]
    # 周四没生成成功时，周六补跑。
    session.delete(issue)
    session.commit()
    code, out = run(THU + timedelta(days=2), "--scheduled")
    assert code == 0 and "草稿已生成" in out
    [catch_up] = reminders()
    assert catch_up.week_start == THIS_WEEK and "周五改过的标题" in catch_up.body_text
    # 下周四是新的一期。
    code, out = run(THU + timedelta(days=7), "--scheduled")
    assert [row.week_start for row in reminders()] == [THIS_WEEK, THIS_WEEK + timedelta(days=7)]
    # 手动生成（不带 --scheduled）覆盖本周草稿；参数校验。
    code, out = run(THU + timedelta(days=7), "--profile", "停用版")
    assert code == 0 and "草稿已生成" in out
    assert run(THU, "--week", "2026-09-28")[0] == 1
    assert run(THU, "--week", "2026-10-05")[0] == 0
    # 周报的定时生成照旧按方案星期筛选。
    monkeypatch.setattr(weekly, "now", lambda: THU)
    assert cli.main(["weekly-digest", "--scheduled"]) == 0
    assert "没有需要生成" in capsys.readouterr().out


def test_crontab_reminder_thursday_after_weekly_same_line():
    path = Path(__file__).resolve().parents[1] / "deploy" / "crontab"
    rows = [
        line.split(None, 5)
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    weekly_rows = [row for row in rows if "job.sh weekly-digest" in row[5]]
    assert [row[4] for row in weekly_rows] == ["1", "2-3", "4-7"]
    assert all(row[:2] == ["30", "7"] for row in weekly_rows)
    command = weekly_rows[-1][5]
    # 共用 job.sh 任务锁：同一行先周报后提醒，不会同时启动互相跳过。
    assert command.index("weekly-digest --scheduled;") < command.index(
        "weekly-digest --kind reminder --scheduled"
    )
    assert sum("--kind reminder" in row[5] for row in rows) == 1


def test_api_reminder_generate_list_detail_regenerate(
    client, session, profile, due_world, monkeypatch
):
    monkeypatch.setattr(weekly, "now", lambda: THU)
    prefix = "/api/admin/weekly"

    def post(path, body=None):
        kwargs = {"json": body} if body is not None else {}
        return client.post(prefix + path, headers=HEADERS, **kwargs)

    assert post("/generate", {"profile_id": profile.id, "kind": "monthly"}).status_code == 422
    stale = post("/generate", {"profile_id": profile.id, "kind": "reminder", "week": "2026-09-28"})
    assert stale.status_code == 422 and "只能生成本周" in stale.json()["detail"]
    created = post("/generate", {"profile_id": profile.id, "kind": "reminder"})
    assert created.status_code == 200 and created.json()["outcome"] == "generated"
    rid = created.json()["issue_id"]
    wid = post("/generate", {"profile_id": profile.id, "week": "2026-10-05"}).json()["issue_id"]
    listed = client.get(prefix + "/issues", headers=HEADERS).json()
    assert listed["total"] == 2
    assert {(item["id"], item["kind"]) for item in listed["items"]} == {
        (rid, "reminder"),
        (wid, "weekly"),
    }
    assert all("body_text" not in item for item in listed["items"])
    only = client.get(prefix + "/issues", params={"kind": "reminder"}, headers=HEADERS).json()
    assert [item["id"] for item in only["items"]] == [rid] and only["total"] == 1
    assert client.get(prefix + "/issues?kind=monthly", headers=HEADERS).status_code == 422
    body = client.get(f"{prefix}/issues/{rid}", headers=HEADERS).json()
    assert body["kind"] == "reminder" and body["counts"] == {"reminder": 6}
    assert body["title"].startswith("申报临期提醒｜")
    assert "https://example.gov.cn/" in body["body_text_with_links"]
    assert "https://" not in body["body_text"] + body["body_html"]
    due_world["第七天截止"].title = "改后的第七天截止"
    session.commit()
    again = post(f"/issues/{rid}/regenerate").json()
    assert again == {"outcome": "generated", "reason": "草稿已生成", "issue_id": rid}
    assert (
        "改后的第七天截止"
        in client.get(f"{prefix}/issues/{rid}", headers=HEADERS).json()["body_text"]
    )
    assert client.get(f"{prefix}/issues/{wid}", headers=HEADERS).json()["kind"] == "weekly"
    # 已生成的提醒里有文件后来下架：预览和标已发布都拦住。
    due_world["第七天截止"].taken_down_at = THU
    session.commit()
    assert client.get(f"{prefix}/issues/{rid}", headers=HEADERS).status_code == 409
    assert post(f"/issues/{rid}/status", {"status": "published"}).status_code == 409


def test_migration_0016_kind_upgrade_downgrade(db_engine, session, profile):
    assert db_engine.url.database.startswith("youju_test")
    for kind in ("weekly", "reminder"):
        session.add(
            DigestIssue(
                profile_id=profile.id,
                kind=kind,
                week_start=THIS_WEEK,
                week_end=THIS_WEEK + timedelta(days=6),
                title=kind,
                body_html="",
                body_text="",
                body_text_with_links="",
                counts={},
                document_ids=[],
            )
        )
    session.commit()
    cfg = alembic_config(db_engine.url.render_as_string(hide_password=False))

    def uniques():
        return {c["name"] for c in inspect(db_engine).get_unique_constraints("digest_issues")}

    try:
        command.downgrade(cfg, "0015")
        columns = {c["name"] for c in inspect(db_engine).get_columns("digest_issues")}
        assert "kind" not in columns
        assert "uq_digest_issue_week" in uniques() and "uq_digest_issue_kind_week" not in uniques()
    finally:
        command.upgrade(cfg, "head")
    session.expire_all()
    [kept] = session.scalars(select(DigestIssue)).all()
    assert (kept.title, kept.kind) == ("weekly", "weekly")
    assert "uq_digest_issue_kind_week" in uniques() and "uq_digest_issue_week" not in uniques()
