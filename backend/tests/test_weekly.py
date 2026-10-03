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
    assert "国家部委\n" in text and text.index("国家部委\n") < text.index("浙江\n")
    assert "发布日期：2026-09-28" in text and "2026-10-10" in text
    assert "2026-10-11" not in text  # 候选日期不能输出
    assert "剩余 1 天" in text and "剩余 30 天" in text
    assert "支持方式：资金支持" in text
    assert "贷款贴息" not in text and "资格认定" not in text
    assert "截止时间见原文" in text
    assert "条数：2" in text and "条数：1" in text
    assert "不可输出" not in text and "条数：4" not in text
    tree = html.fragment_fromstring(issue.body_html)
    assert tree.xpath(".//strong/a[text()='企业名单公示']/@href") == [world["listing"].url]
    assert "企业名单\n发文机关" in text  # 未额外添加公示或公布状态
    assert "科〔2026〕1号" in text
    for word in ("下架项目", "非公开", "已过期", "远期"):
        assert word not in text
    assert text.endswith(weekly.DISCLAIMER)


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
    assert "2026-10-10" not in issue.body_text


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
    assert "共 35 条，其余在「有据」小程序里查看" in issue.body_text


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
    frozen = issue.body_text, issue.generated_at
    world["apply"].title = "再次修改"
    session.commit()
    assert generate(session, profile).outcome == "published"
    assert (issue.body_text, issue.generated_at) == frozen
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
    assert issue.counts["policies"] == 2
    for element in root.iter():
        assert element.tag in {"section", "p", "h2", "h3", "strong", "ul", "li", "a"}
        assert set(element.attrib) <= {"style", "href"}
        if "href" in element.attrib:
            assert element.attrib["href"].startswith("https://example.gov.cn/")
    assert not root.xpath(".//script | .//img | .//*[@class]")
    assert "&lt;script&gt;" in issue.body_html and "&amp;" in issue.body_html
    assert "智能" not in issue.body_text and "AI" not in issue.body_text


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
    assert "早已截止" not in issue.body_text and "剩余 0 天" in issue.body_text
    assert "发布日期：见原文" in issue.body_text


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
