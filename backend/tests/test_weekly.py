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
    # 标题不超过 32 字（官方新建草稿接口）：放不下"30天内截止3项"就只写新通知
    assert issue.title == "申报周报｜9月28日—10月4日（全国）：新通知4条"
    text = issue.body_text
    lines = text.splitlines()
    # 本周新通知又在 30 天内截止的只列一次（列在截止里）
    assert lines.count("项目申报通知") == 1
    assert lines[0] == (
        "第三方整理，非政府平台，以原文为准。统计周：2026 年 9 月 28 日—10 月 4 日（按发布日期）。"
    )
    assert lines[1] == (
        "本期全国新通知 4 条、30 天内截止 3 项（已核对）、名单 2 份、政策文件 1 份。"
        "正文每省列 5 条，全部条目在小程序「本期清单」里，可以按省份看，点进详情可以站内阅读全文。"
    )
    # 块标题不写条数（免得和标题的"新通知4条"被看成对不上）；国家部委在前
    assert lines.index("国家部委") < lines.index("浙江")
    # 本周新发布、又在 30 天内截止的列在截止里，也标"新通知"
    assert lines[lines.index("项目申报通知") + 1] == (
        "新通知 · 企业申报截止 10 月 10 日（10 月 5 日核对） · 浙江科技部门 · 9 月 28 日发布 · "
        "支持方式：资金支持"
    )
    assert lines[lines.index("历史项目申报通知") + 1].startswith("企业申报截止 ")
    assert "看截止、查名单、搜通知：微信小程序「有据申报与行业助手」。" in lines
    assert "10 月 11 日" not in text  # 候选日期不能输出
    assert "企业申报截止 10 月 6 日 17:00（10 月 5 日核对）" in text
    assert "企业申报截止 11 月 4 日（10 月 5 日核对）" in text
    assert "贷款贴息" not in text and "资格认定" not in text
    assert "新通知 · 浙江科技部门 · 9 月 28 日发布 · 截止时间见原文" in text
    # 浙江 8 条：截止 3 条、新通知 2 条进正文，其余在本期清单
    assert "浙江还有 3 条，在本期清单里看。" in lines
    assert "不可输出" not in text and "· 4 条" not in text
    tree = html.fragment_fromstring(issue.body_html)
    assert not tree.xpath(".//a")
    assert "http" not in text and "http" not in issue.body_html
    assert world["apply"].url in issue.body_text_with_links
    for word in ("下架项目", "非公开", "已过期", "远期"):
        assert word not in text
    assert lines[-1] == weekly.FOOT
    assert lines[-6:-1] == [
        "30 天内截止从 10 月 5 日算起，只收人工核对过的日期。",
        weekly.LOCAL_EARLIER,
        "数据截至 2026 年 10 月 5 日 07:30（北京时间）。",
        weekly.GUIDE,
        weekly.CORRECTION,
    ]
    # 每条恰好两个段落，标题加粗，下一段小字；纯文本与 HTML 的行一一对应。
    assert "\n".join(tree.xpath("./*/text() | ./p/strong/text()")) == "\n".join(
        line for line in text.splitlines() if line
    )
    titles = tree.xpath("./p[strong]")
    assert len(titles) == len(issue.document_ids) == 6
    assert all(
        p.getnext().tag == "p" and "font-size:12px" in p.getnext().get("style") for p in titles
    )
    for line in issue.body_text_with_links.splitlines():
        if "｜" in line:
            assert len(line.split("｜")) == 5 and line.split("｜")[-1].startswith("https://")
    # 本期清单存这一期全部条目（正文每省 5 条），按块的顺序，一份文件一条
    titles = {doc.id: doc.title for doc in session.scalars(select(Document))}
    assert [(titles[item["id"]], item["section"]) for item in issue.items] == [
        ("国家项目申报通知", "applications"),
        ("历史项目申报通知", "deadlines"),
        ("项目申报通知", "deadlines"),
        ("边界项目申报通知", "deadlines"),
        ("待核对项目申报通知", "applications"),
        ("正文变化项目申报通知", "applications"),
        ("企业名单公示", "lists"),
        ("企业名单", "lists"),
        ("管理办法", "policies"),
    ]
    assert issue.items[2]["id"] == world["apply"].id  # 副本的截止算在主文件上
    assert {item["province"] for item in issue.items} == {"国家", "浙江"}
    assert set(issue.document_ids) < {item["id"] for item in issue.items}


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


def test_province_blocks_national_first_then_by_count_policies_apart(session, profile):
    # 倒序插入、发布日期和截止日期也故意倒序，避免碰巧按出现顺序通过。
    plan = {"江苏": 1, "浙江": 2, "北京": 2, "安徽": 3, "国家": 2}
    for n, (province, size) in enumerate(plan.items()):
        src = source(session, province)
        for k in range(size):
            kind = ("政策", "名单", "申报通知")[k % 3]
            doc = document(session, src, f"{province}{kind}{k}", START + timedelta(days=n), kind)
            if kind == "申报通知":
                deadline(session, doc, NOW.date() + timedelta(days=5 - n))
    issue = generate(session, profile).issue
    root = html.fragment_fromstring(issue.body_html)
    # 国家部委在前（全国性政策文件另列，块里只剩名单 1 条）；各省按条数从多到少，一样多按省份顺序
    assert root.xpath("./h3/text()") == [
        "国家部委",
        "安徽",
        "北京",
        "浙江",
        "江苏",
        "全国性政策文件",
    ]
    lines = issue.body_text.splitlines()
    # 块内顺序：30 天内截止 → 新申报通知 → 名单 → 政策文件
    block = lines[lines.index("安徽") + 1 : lines.index("北京") - 1]
    assert block[0::2] == ["安徽申报通知2", "安徽名单1", "安徽政策0"]
    assert block[1].startswith("新通知 · 企业申报截止 ")
    assert lines.index("全国性政策文件") > lines.index("江苏")
    titles = {doc.id: doc.title for doc in session.scalars(select(Document))}
    assert [(titles[item["id"]], item["province"]) for item in issue.items] == [
        ("国家名单1", "国家"),
        ("安徽申报通知2", "安徽"),
        ("安徽名单1", "安徽"),
        ("安徽政策0", "安徽"),
        ("北京名单1", "北京"),
        ("北京政策0", "北京"),
        ("浙江名单1", "浙江"),
        ("浙江政策0", "浙江"),
        ("江苏政策0", "江苏"),
        ("国家政策0", "国家"),
    ]


def test_compact_lists_omit_unknown_count_and_unstated_status(session, profile):
    src = source(session)
    for title in ("名单", "名单公示", "名单公布", "公示及公布名单"):
        document(session, src, title, kind="名单")
    issue = generate(session, profile).issue
    lines = issue.body_text.splitlines()
    assert lines[lines.index("名单") + 1] == "名单 · 浙江科技部门 · 9 月 28 日"
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
    # 窗口内的已核对截止都写（企业申报截止在前），本周新通知和截止只列一次
    assert issue.body_text.splitlines()[issue.body_text.splitlines().index("申报 通知") + 1] == (
        "新通知 · 企业申报截止 10 月 10 日 17:00（10 月 5 日核对）；材料报送截止 10 月 12 日（10 月 5 日核对）"
        " · 科技厅 · 9 月 28 日发布"
    )
    assert "https://" not in issue.body_text + issue.body_html
    assert "www." not in issue.body_text + issue.body_html
    assert "申报 通知" in issue.body_text.splitlines()
    linked = [line for line in issue.body_text_with_links.splitlines() if "｜" in line]
    assert len(linked) == 1
    assert "2026-10-10 17:00（2026-10-05 核对）" in linked[0]
    assert "材料报送截止 2026-10-12（2026-10-05 核对）" in linked[0]
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
    # 只开新通知一栏：导语只写这一栏，也不写"30 天内截止从哪天算起"
    assert issue.body_text.splitlines()[1].startswith("本期新通知 2 条。")
    assert "30 天内截止" not in issue.body_text
    assert issue.title == "申报周报｜9月28日—10月4日（全国版）：新通知2条"
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
def test_province_cap_five_overflow_line_and_full_items(session, profile, column, kind):
    src = source(session)
    profile.columns = dict.fromkeys(weekly.COLUMNS, False) | {column: True}
    for n in range(35):
        doc = document(session, src, f"题录 {n:02}", START, kind)
        if column == "deadlines":
            deadline(session, doc, NOW.date() + timedelta(days=n % 30))
    session.commit()
    issue = generate(session, profile).issue
    assert issue.counts[column] == 35
    assert issue.body_text.count("题录 ") == weekly.PER_BLOCK == 5
    assert "浙江" in issue.body_text.splitlines()
    assert "浙江还有 30 条，在本期清单里看。" in issue.body_text.splitlines()
    assert len(issue.document_ids) == 5 and len(issue.items) == 35
    assert {item["section"] for item in issue.items} == {column}


def test_national_policies_listed_apart_with_cap(session, profile):
    national = source(session, "国家")
    for n in range(12):
        document(session, national, f"国家政策 {n:02}", START, "政策")
    session.commit()
    issue = generate(session, profile).issue
    lines = issue.body_text.splitlines()
    assert "国家部委（" not in issue.body_text  # 国家部委只有政策文件：不单独成块
    assert "全国性政策文件" in lines
    assert issue.body_text.count("国家政策 ") == weekly.POLICY_LIMIT == 10
    assert "还有 2 份，在本期清单里看。" in lines
    assert lines[lines.index("国家政策 00") + 1] == (
        "政策文件 · 国家科技部门 · 9 月 28 日发布 · 文号：见原文"
    )
    assert len(issue.items) == 12 and len(issue.document_ids) == 10
    assert issue.title == "申报周报｜9月28日—10月4日（全国）：政策文件12份"


def test_title_span_scope_and_cross_year_week(session, profile):
    src = source(session)
    for day in (date(2025, 12, 29), date(2026, 1, 4)):
        document(session, src, f"跨年通知{day.day}", day)
    session.commit()
    issue = weekly.generate(session, profile.id, date(2025, 12, 29), NOW).issue
    # 跨年两头写年份（放不下统计就只写日期和范围）
    assert issue.title == "申报周报｜2025年12月29日—2026年1月4日（全国）"
    assert issue.body_text.splitlines()[0].endswith(
        "统计周：2025 年 12 月 29 日—2026 年 1 月 4 日（按发布日期）。"
    )
    document(session, src, "同月通知", date(2026, 10, 14))
    session.commit()
    issue = weekly.generate(session, profile.id, date(2026, 10, 12), NOW).issue
    assert issue.title.startswith("申报周报｜10月12日—18日（全国）：")
    # 限了省份或关键词的方案写方案名称，不写"全国"
    profile.provinces = ["浙江"]
    session.commit()
    issue = weekly.generate(session, profile.id, date(2026, 10, 12), NOW).issue
    assert issue.title.startswith("申报周报｜10月12日—18日（全国版）：")
    assert issue.scope == "全国版"
    # 已有数据里限了范围却叫"全国"的方案：补"·限定范围"，不当成全国版
    profile.name = "全国"
    session.commit()
    issue = weekly.generate(session, profile.id, date(2026, 10, 12), NOW).issue
    assert issue.title.startswith("申报周报｜10月12日—18日（全国·限定范围）")
    assert issue.scope == "全国·限定范围"
    profile.name = "全国版"
    session.commit()
    assert issue.body_text.splitlines()[1].startswith("本期新通知 1 条。")


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


def test_weekly_title_fits_wechat_limit_with_as_many_counts_as_fit(session, profile):
    src = source(session)
    document(session, src, "同月短区间通知", date(2026, 10, 14))
    session.commit()
    issue = weekly.generate(session, profile.id, date(2026, 10, 12), NOW).issue
    assert issue.title == "申报周报｜10月12日—18日（全国）：新通知1条"
    # 超长的方案名称一项也放不下时只写日期和范围（管理台提示人工删减）
    profile.name, profile.provinces = "很长的方案名称" * 4, ["浙江"]
    session.commit()
    issue = weekly.generate(session, profile.id, date(2026, 10, 12), NOW).issue
    assert issue.title == f"申报周报｜10月12日—18日（{profile.name}）"
    profile.name, profile.provinces = "全国版", []
    session.commit()
    deadline(session, document(session, src, "截止通知", date(2026, 10, 13)), date(2026, 10, 20))
    session.commit()
    issue = weekly.generate(session, profile.id, date(2026, 10, 12), NOW).issue
    assert issue.title == "申报周报｜10月12日—18日（全国）：新通知2条"
    assert len(issue.title) <= weekly.TITLE_LIMIT
    assert "30 天内截止 1 项（已核对）" in issue.body_text.splitlines()[1]


def test_bare_domains_removed_from_article_kept_in_linked_version(session, profile):
    src = source(session)
    doc = document(
        session, src, "关于 kjt.example.gov.cn/tzgg/1.html 公布的申报通知（ndrc.gov.cn）"
    )
    session.commit()
    issue = generate(session, profile).issue
    for body in (issue.body_text, issue.body_html):
        assert "gov.cn" not in body and "example" not in body
    assert "第三方整理" in issue.body_text and "2.0" not in issue.body_text
    assert doc.title in issue.body_text_with_links  # 带链接版照录原题录


def test_changed_on_private_twin_suppresses_master_deadline(session, profile):
    src = source(session)
    private = source(session, "浙江", 2)
    master = document(session, src, "主文件申报通知", START - timedelta(days=30))
    deadline(session, master, date(2026, 10, 10))
    twin = document(session, private, "主文件申报通知", START - timedelta(days=30))
    session.add(
        DocumentMerge(duplicate_id=twin.id, canonical_id=master.id, match_key="pt", active=True)
    )
    changed = deadline(
        session,
        twin,
        date(2026, 10, 10),
        status="changed",
        change_reason="延期",
        changed_to_document_id=master.id,
    )
    changed.changed_at = NOW
    # 只在非一级来源副本上核对通过的日期：公众号不写（只取一级来源上的），小程序照它自己的口径
    other = document(session, src, "副本才有截止的通知", START - timedelta(days=30))
    deadline(session, other, date(2026, 10, 12))
    other_twin = document(session, private, "副本才有截止的通知", START - timedelta(days=30))
    session.add(
        DocumentMerge(
            duplicate_id=other_twin.id, canonical_id=other.id, match_key="pt2", active=True
        )
    )
    deadline(session, other_twin, date(2026, 10, 14)).kind = "材料报送截止"
    session.commit()
    issue = generate(session, profile).issue
    assert "主文件申报通知" not in issue.body_text  # 副本上已变更：主文件同一截止不再写
    lines = issue.body_text.splitlines()
    assert lines[lines.index("副本才有截止的通知") + 1].startswith(
        "企业申报截止 10 月 12 日（10 月 5 日核对） · "
    )
    assert "10 月 14 日" not in issue.body_text


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
    template_only = issue.title + issue.body_text + issue.body_text_with_links
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
        ("get", "/lists?profile_id=1", None),
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
        {"name": "全国"},  # 限了省份的方案不能叫"全国"
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
    assert "早已截止" not in issue.body_text
    assert "企业申报截止 10 月 5 日 08:00（10 月 5 日核对）" in issue.body_text
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
    assert issue.title == "临期提醒｜10月8日—15日截止的6个申报项目（已核对）"
    assert len(issue.title) <= weekly.TITLE_LIMIT == 32
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
    assert lines[:3] == [
        "第三方整理，非政府平台，以原文为准。截止从 2026 年 10 月 8 日算起，只列人工核对过的日期。",
        "",
        "10 月 8 日—15 日截止的申报项目（6 项）",
    ]
    assert lines[lines.index("今天下午截止") + 1] == (
        "浙江 · 浙江科技部门 · 企业申报截止 10 月 8 日 17:00（10 月 5 日核对）"
    )
    assert lines[lines.index("归并主文件截止") + 1] == (
        "浙江 · 省科技厅 · 企业申报截止 10 月 13 日（10 月 5 日核对）"
    )
    assert lines[lines.index("只有材料报送截止") + 1] == (
        "浙江 · 浙江科技部门 · 材料报送截止 10 月 14 日（10 月 5 日核对）"
    )
    assert lines[lines.index("国家三天后截止") + 1].startswith("国家部委 · 国家科技部门 · ")
    assert "仅列" not in text and "只列前" not in text  # 不到 10 项不写
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
        assert element.tag in {"section", "p", "h3", "strong"}
        assert set(element.attrib) <= {"style"}
    assert not tree.xpath(".//a | .//h2")
    assert "http" not in issue.body_text and "http" not in issue.body_html
    # 公众号版：每条加粗标题加一行小字；纯文本与 HTML 的行一一对应。
    assert "\n".join(tree.xpath("./*/text() | ./p/strong/text()")) == "\n".join(
        line for line in issue.body_text.splitlines() if line
    )
    titles = tree.xpath("./p[strong]")
    assert [p.findtext("strong") for p in titles] == REMINDER_ORDER
    assert all("font-size:12px" in p.getnext().get("style") for p in titles)
    assert issue.body_text.splitlines()[0].startswith(weekly.HEAD)
    assert issue.body_text.splitlines()[-1] == weekly.FOOT
    # 带链接版：每条一行四列，写完整日期、截止类型和核对日期。
    rows = linked_rows(issue)
    assert all(len(row) == 4 and row[-1].startswith("https://example.gov.cn/") for row in rows)
    assert rows[0] == [
        "今天下午截止",
        "浙江科技部门",
        "企业申报截止 2026-10-08 17:00（2026-10-05 核对）",
        due_world["今天下午截止"].url,
    ]
    assert rows[4][2] == "材料报送截止 2026-10-14（2026-10-05 核对）"
    assert issue.body_text_with_links.splitlines()[-1] == weekly.FOOT
    template_only = issue.title + issue.body_text + issue.body_text_with_links
    for value in (*REMINDER_ORDER, "浙江科技部门", "国家科技部门", "省科技厅"):
        template_only = template_only.replace(value, "")
    assert not weekly.FORBIDDEN.search(template_only)


def test_reminder_cross_year_title_drops_years_to_fit(session, profile):
    src = source(session)
    stamp = datetime(2026, 12, 28, 7, 30, tzinfo=weekly.BEIJING)
    for n in range(12):
        deadline(session, document(session, src, f"跨年截止 {n:02}", START), date(2026, 12, 30))
    session.commit()
    issue = weekly.generate(session, profile.id, None, stamp, "reminder").issue
    # 带年份是 41 字，超过 32 字：去掉年份（正文首行写了完整日期）
    assert issue.title == "临期提醒｜12月28日—1月4日截止的12个申报项目（已核对）"
    assert "截止从 2026 年 12 月 28 日算起" in issue.body_text.splitlines()[0]
    assert weekly.LOCAL_EARLIER in issue.body_text.splitlines()


def test_reminder_empty_still_generates_one_sentence(session, profile, due_world):
    profile.keywords = ["没有这样的标题"]
    session.commit()
    result = reminder(session, profile)
    issue = result.issue
    assert result.outcome == "generated" and issue.counts == {"reminder": 0}
    assert issue.document_ids == [] and issue.items == []
    # 方案筛了关键词：标题写方案名称，不写"全国"
    assert issue.title == "临期提醒｜10月8日—15日：没有已核对的截止项目（全国版）"
    lines = issue.body_text.splitlines()
    assert lines[1] == weekly.REMINDER_EMPTY and lines[2] == ""
    assert lines[-1] == weekly.FOOT and "项）" not in issue.body_text
    assert issue.body_text_with_links.splitlines() == lines
    assert weekly.REMINDER_EMPTY == "本周未来 7 天没有已核对截止的申报项目"
    tree = html.fragment_fromstring(issue.body_html)
    assert tree.xpath("./p/text()") == [r for r in lines if r]
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
    # 超过 10 项只列前 10 项，开头写明，完整清单在小程序截止页（方案第四节第 4 条）
    assert issue.counts == {"reminder": 35} and len(issue.document_ids) == 10
    assert issue.body_text.count("题录 ") == weekly.REMINDER_LIMIT == 10
    assert issue.body_text.splitlines()[1] == (
        "共 35 项，这里按截止日期先后只列前 10 项，完整清单在小程序「截止」页。"
    )
    assert issue.title == "临期提醒｜10月8日—15日截止的35个申报项目（全国版，已核对）"


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
    assert again.title.startswith("临期提醒｜10月11日—18日截止的")
    assert again.title.endswith("个申报项目（已核对）")
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
    assert body["title"].startswith("临期提醒｜")
    assert body["paths"] == [
        {
            "label": "截止页（7 天内，只看已核对）",
            "path": "pages/report/index?days=7&verified=1",
            "published_only": False,
        }
    ]
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
    assert (kept.title, kept.kind, kept.subject_id) == ("weekly", "weekly", 0)
    assert uniques() == {"uq_digest_issue_subject"}


# ---- 名单速报：大名单公布时在管理台选一份名单生成（PR #287 第 3 件事）----

FLASH_TITLE = "关于2026年第一批高新技术企业拟认定名单的公示"
PERIOD = "2026年9月28日至10月9日"


@pytest.fixture
def lists_world(session, profile):
    src = source(session)
    beijing = source(session, "北京")
    private = source(session, "北京", 3)
    big = document(session, src, FLASH_TITLE, START, "名单")
    twin = document(session, src, FLASH_TITLE, START, "名单")
    session.add(
        DocumentMerge(duplicate_id=twin.id, canonical_id=big.id, match_key="flash", active=True)
    )
    entries(session, big, ("不可输出企业甲", "不可输出企业乙", "不可输出企业丙"))
    entries(session, twin, ("不可输出企业甲", "不可输出企业丁"))
    final = document(session, beijing, "2026年专精特新中小企业名单", START + timedelta(1), "名单")
    entries(session, final, ("不可输出企业戊",))
    old = document(session, src, "去年的名单公示", START - timedelta(days=60), "名单")
    hidden = document(session, src, "已下架名单公示", START, "名单")
    hidden.taken_down_at = NOW
    denied = document(session, private, "非公开名单公示", START, "名单")
    notice = document(session, src, "申报通知不是名单", START)
    session.commit()
    return {
        "big": big,
        "twin": twin,
        "final": final,
        "old": old,
        "hidden": hidden,
        "denied": denied,
        "notice": notice,
    }


def flash(session, profile, subject, period=None, stamp=NOW):
    return weekly.generate(
        session, profile.id, None, stamp, "list_flash", subject_id=subject.id, period=period
    )


def test_list_flash_title_period_count_and_how_to_check(session, profile, lists_world):
    big = lists_world["big"]
    # 选到副本也写归并主文件；条数按名称去重、跨归并网址只计一次
    result = flash(session, profile, lists_world["twin"], PERIOD)
    issue = result.issue
    assert result.outcome == "generated"
    assert (issue.kind, issue.subject_id, issue.week_start) == ("list_flash", big.id, START)
    # 照录原标题，括号里补地区和条数
    assert issue.title == f"名单速报｜{FLASH_TITLE}（浙江·共4家）"
    assert issue.counts == {"entries": 4} and issue.document_ids == [big.id]
    assert issue.items == [{"id": big.id, "section": "lists", "province": "浙江", "period": PERIOD}]
    lines = issue.body_text.splitlines()
    assert lines[:7] == [
        weekly.HEAD,
        "",
        FLASH_TITLE,
        "浙江科技部门 · 9 月 28 日 · 公示",
        f"公示期：{PERIOD}（照原文填写）",
        "名单条数：4 家（按名称去重，以原文为准）",
        "",
    ]
    # 查名单用中性说法（D-090）；看原文在小程序里（10/23 审核退回后一律站内阅读，D-089、D-092）
    assert lines[7] == "在政府公开名单里查企业"
    assert lines[8] == (
        "打开小程序「查名单」，输入企业全称，在已收录的政府公开名单里查；查到时显示原名单里的那一行。"
    )
    assert lines[9] == (
        "看详情：在小程序里打开这份名单的详情，点「站内阅读全文」；名单里的企业在「查名单」里查。"
    )
    for word in ("浏览器", "复制原文链接", "上过哪些", "在不在名单", "信用代码"):
        assert word not in issue.body_text + issue.body_html
    assert lines[-1] == weekly.FOOT
    assert "不可输出" not in issue.body_text + issue.body_html + issue.body_text_with_links
    assert "http" not in issue.body_text + issue.body_html
    assert f"{FLASH_TITLE}｜浙江科技部门｜2026-09-28｜{big.url}" in issue.body_text_with_links
    tree = html.fragment_fromstring(issue.body_html)
    for element in tree.iter():
        assert element.tag in {"section", "p", "h3", "strong"}
        assert set(element.attrib) <= {"style"}
    assert "\n".join(tree.xpath("./*/text() | ./p/strong/text()")) == "\n".join(
        line for line in lines if line
    )
    template_only = (issue.title + issue.body_text + issue.body_text_with_links).replace(
        FLASH_TITLE, ""
    )
    assert not weekly.FORBIDDEN.search(template_only)
    # 公布名单没有公示期；没填公示期的公示名单写"见原文"；条数取不到写"见原文"
    final = flash(session, profile, lists_world["final"]).issue
    assert final.title == "名单速报｜2026年专精特新中小企业名单（北京·共1家）"
    assert "公示期" not in final.body_text
    assert "北京科技部门 · 9 月 29 日" in final.body_text.splitlines()
    bare = document(session, session.get(Source, big.source_id), "又一份名单公示", START, "名单")
    session.commit()
    bare_issue = flash(session, profile, bare).issue
    assert bare_issue.title == "名单速报｜又一份名单公示（浙江）"
    # 名单速报不写截止，文末不加"初审可能更早"那句
    assert weekly.LOCAL_EARLIER not in bare_issue.body_text
    assert "公示期：见原文" in bare_issue.body_text.splitlines()
    assert "名单条数：见原文" in bare_issue.body_text.splitlines()


def test_list_flash_one_per_list_period_kept_published_frozen_takedown_removes_draft(
    session, profile, lists_world
):
    first = flash(session, profile, lists_world["big"], PERIOD).issue
    # 重新生成（不传公示期）沿用上次填的；换一份名单是另一篇，同一周也可以有几篇
    again = flash(session, profile, lists_world["big"]).issue
    assert again.id == first.id and again.items[0]["period"] == PERIOD
    changed = flash(session, profile, lists_world["big"], "见原文附件").issue
    assert changed.id == first.id and "公示期：见原文附件（照原文填写）" in changed.body_text
    other = flash(session, profile, lists_world["final"]).issue
    assert other.id != first.id and other.week_start == first.week_start
    weekly_issue = generate(session, profile).issue
    assert (weekly_issue.kind, weekly_issue.subject_id) == ("weekly", 0)
    first.status = "published"
    session.commit()
    frozen = first.body_text
    lists_world["big"].title = "改过的标题"
    session.commit()
    assert flash(session, profile, lists_world["big"]).outcome == "published"
    assert first.body_text == frozen
    first.status = "draft"
    session.commit()
    # 名单后来下架：和周报一样删掉草稿，不留可复制的旧题录
    lists_world["twin"].taken_down_at = NOW
    session.commit()
    gone = flash(session, profile, lists_world["big"])
    assert gone.outcome == "empty" and gone.issue is None
    assert "原来的草稿已删除" in gone.reason
    assert session.get(DigestIssue, first.id) is None
    assert session.get(DigestIssue, other.id) is not None


def test_list_flash_rejects_unlisted_documents_and_cleans_period(session, profile, lists_world):
    with pytest.raises(ValueError, match="请选择"):
        weekly.generate(session, profile.id, None, NOW, "list_flash")
    for key in ("notice", "hidden", "denied"):
        with pytest.raises(ValueError, match="不是名单、已下架、来源不公开"):
            flash(session, profile, lists_world[key])
    # 列个人的名单：人工改成了"名单"也不写速报，也不进候选
    person = document(
        session,
        session.get(Source, lists_world["big"].source_id),
        "2026年职称评审结果公示",
        START,
        "名单",
    )
    session.commit()
    with pytest.raises(ValueError, match="列的是个人"):
        flash(session, profile, person)
    for title in ("2026年评审专家名单", "政府采购供应商名单", "2026年高层次人才名单"):
        other = document(
            session, session.get(Source, lists_world["big"].source_id), title, START, "名单"
        )
        session.commit()
        with pytest.raises(ValueError, match="列的是个人"):
            flash(session, profile, other)
    assert person.id not in [item["id"] for item in weekly.flash_candidates(session, profile, NOW)]
    # 不进查名单的名单（失信、处罚等惩戒类，获奖、表彰类，D-091 ③）也不写速报、不进候选：
    # 速报教读者去查名单里查，这些查不到；惩戒、信用类写速报也像在做企业征信
    for title in (
        "2026年度失信被执行企业名单",
        "2026年无失信记录企业名单公示",
        "2026年度科学技术奖获奖名单",
        "2026年先进集体表彰名单",
    ):
        other = document(
            session, session.get(Source, lists_world["big"].source_id), title, START, "名单"
        )
        session.commit()
        with pytest.raises(ValueError, match="不进查名单"):
            flash(session, profile, other)
        assert other.id not in [
            item["id"] for item in weekly.flash_candidates(session, profile, NOW)
        ]
    profile.provinces = ["北京"]
    session.commit()
    with pytest.raises(ValueError, match="地区和关键词"):
        flash(session, profile, lists_world["big"])
    assert flash(session, profile, lists_world["final"]).outcome == "generated"
    profile.provinces, profile.keywords = [], ["高新技术"]
    session.commit()
    # 已有草稿的名单后来不在方案范围内：删掉草稿（和下架一样），不报错
    gone = flash(session, profile, lists_world["final"])
    assert gone.outcome == "empty" and "地区和关键词" in gone.reason
    issue = flash(
        session, profile, lists_world["big"], "详见 https://example.org/x 第 3 页\n"
    ).issue
    assert issue.items[0]["period"] == "详见 第 3 页"
    assert [i.subject_id for i in session.scalars(select(DigestIssue))] == [lists_world["big"].id]


def test_flash_candidates_recent_public_lists_by_entry_count(session, profile, lists_world):
    items = weekly.flash_candidates(session, profile, NOW)
    assert [item["id"] for item in items] == [lists_world["big"].id, lists_world["final"].id]
    assert items[0]["entries"] == 4 and items[0]["province"] == "浙江"
    assert items[0]["issue_id"] is None
    issue = flash(session, profile, lists_world["big"]).issue
    items = weekly.flash_candidates(session, profile, NOW)
    assert (items[0]["issue_id"], items[0]["issue_status"]) == (issue.id, "draft")
    # 14 天以前发布的不列
    assert weekly.flash_candidates(session, profile, NOW + timedelta(days=30)) == []


def test_api_list_flash_lists_generate_paths_and_period(
    client, session, profile, lists_world, monkeypatch
):
    seen = []
    monkeypatch.setattr(admin.audit_log, "info", lambda value: seen.append(value))
    prefix = "/api/admin/weekly"

    def post(path, body=None):
        kwargs = {"json": body} if body is not None else {}
        return client.post(prefix + path, headers=HEADERS, **kwargs)

    listed = client.get(f"{prefix}/lists?profile_id={profile.id}", headers=HEADERS).json()
    assert [item["id"] for item in listed["items"]][:1] == [lists_world["big"].id]
    assert client.get(f"{prefix}/lists?profile_id=999", headers=HEADERS).status_code == 404
    base = {"profile_id": profile.id, "kind": "list_flash"}
    missing = post("/generate", base)
    assert missing.status_code == 422 and "请选择" in missing.json()["detail"]
    big = {"subject_id": lists_world["big"].id}
    assert post("/generate", base | big | {"period": "长" * 61}).status_code == 422
    # 填了公示期要勾"已对照原文核对"
    unchecked = post("/generate", base | big | {"period": PERIOD})
    assert unchecked.status_code == 422 and "对照原文核对" in unchecked.json()["detail"]
    created = post("/generate", base | big | {"period": PERIOD, "period_confirmed": True})
    assert created.status_code == 200 and created.json()["outcome"] == "generated"
    fid = created.json()["issue_id"]
    body = client.get(f"{prefix}/issues/{fid}", headers=HEADERS).json()
    assert (body["kind"], body["subject_id"], body["period"]) == (
        "list_flash",
        lists_world["big"].id,
        PERIOD,
    )
    assert body["paths"] == [
        {
            "label": "这份名单",
            "path": f"pages/detail/index?id={lists_world['big'].id}",
            "published_only": False,
        },
        {"label": "查名单", "path": "pages/lists/index", "published_only": False},
    ]
    # 重新生成和不填公示期再生成，都沿用上次填的
    assert post(f"/issues/{fid}/regenerate").json()["outcome"] == "generated"
    assert (
        post("/generate", base | {"subject_id": lists_world["big"].id, "period": " "}).json()[
            "issue_id"
        ]
        == fid
    )
    assert client.get(f"{prefix}/issues/{fid}", headers=HEADERS).json()["period"] == PERIOD
    wid = post("/generate", {"profile_id": profile.id, "week": START.isoformat()}).json()[
        "issue_id"
    ]
    weekly_body = client.get(f"{prefix}/issues/{wid}", headers=HEADERS).json()
    assert weekly_body["paths"][0] == {
        "label": "本期清单",
        "path": f"pages/digest/index?id={wid}",
        "published_only": True,
    }
    assert [path["path"] for path in weekly_body["paths"][1:]] == [
        "pages/report/index",
        "pages/lists/index",
        "pages/home/index",
    ]
    assert weekly_body["period"] == ""
    assert any("weekly_lists" in record for record in seen)
    big_id = lists_world["big"].id
    assert any(
        f'"subject_id": {big_id}' in record and '"period": true' in record for record in seen
    )


def test_migration_0033_list_flash_upgrade_downgrade(db_engine, session, profile, lists_world):
    assert db_engine.url.database.startswith("youju_test")
    weekly_issue = generate(session, profile).issue
    # 已有数据里限了地区却叫"全国"的方案：回填时不能算全国版
    odd = DigestProfile(name="全国", provinces=["浙江"])
    session.add(odd)
    session.commit()
    odd_issue = weekly.generate(session, odd.id, START, NOW).issue
    flash(session, profile, lists_world["big"], PERIOD)
    cfg = alembic_config(db_engine.url.render_as_string(hide_password=False))

    def shape():
        columns = {c["name"] for c in inspect(db_engine).get_columns("digest_issues")}
        uniques = {c["name"] for c in inspect(db_engine).get_unique_constraints("digest_issues")}
        return columns, uniques

    try:
        command.downgrade(cfg, "0041")
        columns, uniques = shape()
        assert not {"subject_id", "items", "scope"} & columns
        assert uniques == {"uq_digest_issue_kind_week"}
    finally:
        command.upgrade(cfg, "head")
    session.expire_all()
    # 回退只删名单速报，周报保留；升级后旧期刊没有 items（本期清单按正文里的文件给）
    kept, odd_kept = session.scalars(select(DigestIssue).order_by(DigestIssue.id)).all()
    assert (kept.id, kept.kind, kept.subject_id, kept.items) == (weekly_issue.id, "weekly", 0, None)
    # 升级时按方案回填范围：升级前发布的全国版周报，"本周周报"也找得到；叫"全国"的地区版不算
    assert kept.scope == "全国"
    assert (odd_kept.id, odd_kept.scope) == (odd_issue.id, "全国·限定范围")
    columns, uniques = shape()
    assert {"subject_id", "items", "scope"} <= columns and uniques == {"uq_digest_issue_subject"}
