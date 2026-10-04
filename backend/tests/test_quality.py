"""数据质量抽检表：抽样确定性和分层、日期匹配、证据逐字核对、名单近似名、CLI 和管理接口。"""

import csv
import hashlib
import io
import random
import re
from datetime import UTC, date, datetime, time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from youju import cli, quality
from youju.api import app as admin
from youju.db import get_session
from youju.lists.rules import normalize_name
from youju.models import (
    Document,
    DocumentDeadline,
    DocumentDetail,
    DocumentField,
    ListEntry,
    Source,
)
from youju.quality import (
    COMPARE_HEADERS,
    NOTICE_HEADERS,
    date_in_text,
    evidence_in_body,
    near_variant,
    sample_compare,
    sample_notices,
)

HEADERS = {"X-Admin-Token": "quality-test-token"}
APPLY = "关于组织开展2026年{}项目申报工作的通知"
BODY = (
    "各有关单位：\n为支持企业研发，现组织开展项目申报。\n"
    "一、支持方式\n对通过评审的项目给予最高50万元资金支持。\n"
    "二、申报时间\n企业申报截止时间为2026年10月30日 17:00，逾期不予受理。\n"
    "请各市主管部门于11月5日前完成推荐。\n"
)


def _source(session, name, rights_level=1):
    src = Source(
        level="省级",
        province="山东",
        dept="科技",
        dept_full_name=f"{name}科技厅",
        column_name="通知公告",
        url=f"https://{name}.example.gov.cn/tz/",
        rights_level=rights_level,
    )
    session.add(src)
    session.flush()
    return src


def _doc(session, src, n, title=None, url=None, taken_down=False):
    now = datetime.now(UTC)
    doc = Document(
        source_id=src.id,
        url=url or f"https://{src.url.split('/')[2]}/{n}.html",
        url_key=f"{src.url.split('/')[2]}/{n}.html",
        title=title or APPLY.format(f"第{n}号"),
        publish_date=date(2026, 9, 20),
        first_seen_at=now,
        last_seen_at=now,
        taken_down_at=now if taken_down else None,
    )
    session.add(doc)
    session.flush()
    return doc


def _detail(session, doc, body=BODY):
    session.add(
        DocumentDetail(
            document_id=doc.id,
            body_text=body,
            issuer="示例科技厅",
            doc_number="示科发〔2026〕8号",
            fetched_at=datetime.now(UTC),
            status="ok",
        )
    )


def _deadline(session, doc, key, day, evidence, status="approved", at=None):
    session.add(
        DocumentDeadline(
            document_id=doc.id,
            fingerprint=key,
            kind="企业申报截止",
            candidate_date=day,
            candidate_time=at,
            evidence=evidence,
            confidence="高",
            status=status,
            approved_date=day if status == "approved" else None,
            approved_time=at if status == "approved" else None,
            reviewed_by="测试",
            reviewed_at=datetime.now(UTC),
        )
    )


def _field(session, doc, value, evidence, body=BODY):
    session.add(
        DocumentField(
            document_id=doc.id,
            field="support",
            value=value,
            evidence=evidence,
            evidence_offset=0,
            body_hash=hashlib.sha256(body.encode()).hexdigest(),
            status="approved",
        )
    )


def _by_id(rows):
    return {row["文件编号"]: row for row in rows}


# ---------------------------------------------------------------- 纯函数


@pytest.mark.parametrize(
    "text",
    [
        "企业申报截止时间为2026年10月30日",
        "截止 2026 年 10 月 30 日 17:00",
        "2026-10-30",
        "2026/10/30前",
        "2026.10.30",
        "请于10月30日前提交",
        "请于10月30号前提交",
        "截止日期：2026年10月30",
        "１０月３０日",
        "2026年10月30日（星期五）",
    ],
)
def test_date_in_text_common_formats(text):
    assert date_in_text(date(2026, 10, 30), text)


def test_date_in_text_leading_zeros():
    assert date_in_text(date(2026, 1, 3), "2026年01月03日")
    assert date_in_text(date(2026, 1, 3), "2026-01-03")
    assert date_in_text(date(2026, 1, 3), "1月3日前")


@pytest.mark.parametrize(
    "day,text",
    [
        (date(2026, 10, 3), "10月30日"),  # 日后面还有数字
        (date(2026, 1, 3), "2026-11-30"),
        (date(2026, 1, 3), "1月30日"),
        (date(2026, 10, 30), "110月30日"),  # 月前面还有数字
        (date(2026, 10, 30), "10月301日"),
        (date(2026, 10, 30), "2025年10月30日"),  # 年份写明了但不对
        (date(2026, 10, 30), "2026年11月30日"),
        (date(2026, 10, 30), "110.30万元"),
        (date(2026, 10, 30), "电话0531-86123456"),
        (date(2026, 10, 30), "没有日期"),
    ],
)
def test_date_in_text_does_not_match_neighbouring_digits(day, text):
    assert not date_in_text(day, text)


def test_evidence_in_body_only_collapses_whitespace():
    body = "二、申报时间\n企业申报截止时间为2026年10月30日　 17:00，逾期不予受理。"
    assert evidence_in_body("企业申报截止时间为2026年10月30日 17:00", [body]) is True
    assert evidence_in_body("企业申报截止时间为 2026年10月30日  17:00", [body]) is False
    assert evidence_in_body("企业申报截止时间为2026年10月30日17:00", [body]) is False
    assert evidence_in_body("企业申报截止时间为2026年10月31日 17:00", [body]) is False
    assert evidence_in_body("逾期不予受理", ["别的正文", body]) is True
    assert evidence_in_body("逾期不予受理", []) is None
    assert evidence_in_body("  ", [body]) is False


def test_near_variant_differs_from_original_and_taken():
    names = [
        "北京星河科技有限公司",
        "深圳市华为技术股份有限公司",
        "中国科学院自动化研究所",
        "星河公司",
        "上海（浦东）新材料研究中心",
        "山东省农业科学院",
        "青岛啤酒股份有限公司青岛分公司",
    ]
    for seed in range(30):
        rng = random.Random(seed)
        taken = {normalize_name(n) for n in names}
        for name in names:
            made = near_variant(name, rng, taken)
            assert made is not None
            variant, method = made
            assert normalize_name(variant) != normalize_name(name)
            assert normalize_name(variant) not in taken
            assert method in {m for m, _ in quality.NEAR_METHODS}
            taken.add(normalize_name(variant))


def test_near_variant_skips_existing_names():
    # 每种生成方式都可能撞上已有名称：已有的不能当作近似名
    rng = random.Random(5)
    base = "北京星河科技有限公司"
    taken = {base}
    seen = set()
    for _ in range(20):
        variant, _method = near_variant(base, rng, taken)
        assert variant not in taken
        seen.add(variant)
        taken.add(variant)
    assert len(seen) == 20


# ---------------------------------------------------------------- 通知抽样


def test_notice_sample_is_deterministic_and_stratified(session):
    big, small, single = _source(session, "a"), _source(session, "b"), _source(session, "c")
    n = 0
    for src, count in ((big, 10), (small, 2), (single, 1)):
        for _ in range(count):
            n += 1
            _doc(session, src, n)
    session.commit()

    first = sample_notices(session, n=6, seed=3)
    assert first == sample_notices(session, n=6, seed=3)
    assert len(first) == 6 and [r["序号"] for r in first] == list(range(1, 7))
    counts = {}
    for row in first:
        counts[row["来源名称"]] = counts.get(row["来源名称"], 0) + 1
    # 轮流抽：每个来源都有，少的来源抽完为止
    assert counts == {"a科技厅 · 通知公告": 3, "b科技厅 · 通知公告": 2, "c科技厅 · 通知公告": 1}
    orders = {tuple(r["文件编号"] for r in sample_notices(session, n=6, seed=s)) for s in range(8)}
    assert len(orders) > 1
    assert len(sample_notices(session, n=50, seed=1)) == 13


def test_notice_checks_and_scope(session):
    src, other, private = _source(session, "a"), _source(session, "b"), _source(session, "p", 2)
    good = _doc(session, src, 1)
    _detail(session, good)
    _deadline(
        session,
        good,
        "g1",
        date(2026, 10, 30),
        "企业申报截止时间为2026年10月30日 17:00",
        at=time(17),
    )
    _deadline(
        session, good, "g2", date(2026, 11, 5), "请各市主管部门于11月5日前完成推荐", "pending"
    )
    _deadline(session, good, "g3", date(2026, 11, 6), "逾期不予受理", "rejected")
    _field(session, good, "资金支持", "对通过评审的项目给予最高50万元资金支持")

    missing = _doc(session, src, 2)
    _detail(session, missing)
    _deadline(session, missing, "m1", date(2026, 10, 30), "企业网上申报截止时间为2026年10月30日")

    wrong_day = _doc(session, src, 3)
    _detail(session, wrong_day)
    _deadline(session, wrong_day, "w1", date(2026, 11, 1), "企业申报截止时间为2026年10月30日 17:00")

    no_body = _doc(session, other, 4)
    _deadline(session, no_body, "n1", date(2026, 10, 30), "截止时间为2026年10月30日")

    bad_field = _doc(session, other, 5)
    _detail(session, bad_field)
    _field(session, bad_field, "贷款贴息", "按贷款利息的50%给予贴息")

    bad_url = _doc(session, other, 6, url="/info/6.html")

    removed = _doc(session, src, 7, taken_down=True)
    _detail(session, removed)
    hidden = _doc(session, private, 8)
    listing = _doc(session, src, 9, title="关于公布2026年高新技术企业名单的通知")
    session.commit()

    rows = sample_notices(session, n=200, seed=1)
    by_id = _by_id(rows)
    assert set(by_id) == {good.id, missing.id, wrong_day.id, no_body.id, bad_field.id, bad_url.id}
    assert {removed.id, hidden.id, listing.id}.isdisjoint(by_id)

    row = by_id[good.id]
    # 只取核对通过的截止：待核对、驳回的不出现
    assert row["截止日期（已核对）"] == "企业申报截止 2026-10-30 17:00"
    assert row["截止证据原句"] == "企业申报截止：企业申报截止时间为2026年10月30日 17:00"
    assert "11月5日" not in row["截止证据原句"]
    assert row["已核对字段"] == "支持方式：资金支持"
    assert row["字段证据原句"] == "资金支持：对通过评审的项目给予最高50万元资金支持"
    assert row["正文"] == f"有（{len(BODY)} 字）"
    assert row["发文机关"] == "示例科技厅" and row["文号"] == "示科发〔2026〕8号"
    assert row["核对①来源可追溯"] == "通过"
    assert row["核对②截止证据"] == "通过"
    assert row["核对③字段证据"] == "通过"
    assert row["程序核对"] == "通过"
    assert row["人工结论"] == "" and row["备注"] == ""

    assert by_id[missing.id]["核对②截止证据"] == (
        "不通过：企业申报截止 2026-10-30：证据原句在正文里找不到"
    )
    assert by_id[missing.id]["程序核对"].startswith("不通过（②")
    assert by_id[wrong_day.id]["核对②截止证据"] == (
        "不通过：企业申报截止 2026-11-01：证据原句里找不到这个日期"
    )
    assert by_id[no_body.id]["正文"] == "无"
    assert by_id[no_body.id]["核对②截止证据"].startswith("无法判断")
    assert by_id[no_body.id]["核对③字段证据"] == "不适用：没有已核对的字段"
    assert by_id[no_body.id]["程序核对"].startswith("无法判断（②")
    assert by_id[bad_field.id]["核对③字段证据"] == (
        "不通过：支持方式 贷款贴息：证据原句在正文里找不到"
    )
    assert by_id[bad_field.id]["核对②截止证据"] == "不适用：没有已核对的截止日期"
    assert by_id[bad_url.id]["核对①来源可追溯"].startswith("不通过")
    assert by_id[bad_url.id]["程序核对"].startswith("不通过（①")


def test_field_with_stale_body_is_not_shown(session):
    src = _source(session, "a")
    doc = _doc(session, src, 1)
    _detail(session, doc)
    _field(session, doc, "资金支持", "给予最高50万元资金支持", body="旧正文")
    session.commit()
    row = sample_notices(session, n=5, seed=1)[0]
    assert row["已核对字段"] == "" and row["核对③字段证据"].startswith("不适用")


# ---------------------------------------------------------------- 名单比对抽样


def _list_doc(session, src, n, names, title="关于公布2026年高新技术企业名单的通知"):
    doc = _doc(session, src, 100 + n, title=title)
    for i, name in enumerate(names, 1):
        session.add(
            ListEntry(
                document_id=doc.id,
                source_url=doc.url,
                name=name,
                normalized_name=normalize_name(name),
                sequence=str(i),
                category="高新技术企业",
            )
        )
    return doc


def test_compare_sample_real_and_near(session):
    src, private = _source(session, "a"), _source(session, "p", 2)
    _list_doc(session, src, 1, ["北京星河科技有限公司", "上海云帆信息技术有限公司"])
    _list_doc(session, src, 2, ["深圳海纳智能装备股份有限公司", "杭州青禾生物医药有限公司"])
    _list_doc(session, private, 3, ["不公开名单里的企业有限公司"])
    session.commit()

    rows = sample_compare(session, n=4, seed=2)
    assert rows == sample_compare(session, n=4, seed=2)
    assert [r["是否真实名"] for r in rows] == ["是", "是", "否", "否"]
    public = {
        "北京星河科技有限公司",
        "上海云帆信息技术有限公司",
        "深圳海纳智能装备股份有限公司",
        "杭州青禾生物医药有限公司",
    }
    for row in rows[:2]:
        assert row["输入名称"] in public
        assert row["比对结果"] == "命中" and row["命中的名称"] == row["输入名称"]
        assert row["命中名单"].startswith("#") and "高新技术企业名单" in row["命中名单"]
        assert row["程序判断"] == "通过"
    for row in rows[2:]:
        assert normalize_name(row["输入名称"]) not in {normalize_name(n) for n in public}
        assert row["近似名依据（真实名称）"] in public
        assert row["近似名生成方式"]
        assert row["比对结果"] == "未命中" and row["程序判断"] == "通过"
    # 两个真实名分别来自两份名单（按名单分层）
    assert len({r["命中名单"] for r in rows[:2]}) == 2
    assert all(r["输入名称"] != "不公开名单里的企业有限公司" for r in rows)


def test_compare_judgments(session, monkeypatch):
    src = _source(session, "a")
    _list_doc(session, src, 1, ["北京星河科技有限公司", "上海云帆信息技术有限公司"])
    session.commit()

    def fake(_session, value):
        if value in ("北京星河科技有限公司", "上海云帆信息技术有限公司"):
            return "未命中", []
        return "命中", [(1, "某个不同的企业有限公司")]

    monkeypatch.setattr(quality, "_lookup_row", fake)
    rows = sample_compare(session, n=4, seed=1)
    assert {r["程序判断"] for r in rows if r["是否真实名"] == "是"} == {"漏查待人工看"}
    assert {r["程序判断"] for r in rows if r["是否真实名"] == "否"} == {"可能误匹配待人工看"}


# ---------------------------------------------------------------- CLI 和管理接口


def _csv_rows(data: bytes):
    assert data.startswith(b"\xef\xbb\xbf")
    return list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))


@pytest.mark.parametrize(
    "kind,headers", [("notices", NOTICE_HEADERS), ("compare", COMPARE_HEADERS)]
)
def test_cli_writes_bom_csv(session, session_factory, monkeypatch, tmp_path, capsys, kind, headers):
    src = _source(session, "a")
    for n in (1, 3):
        _detail(session, _doc(session, src, n))
    _list_doc(session, src, 2, ["北京星河科技有限公司", "上海云帆信息技术有限公司"])
    session.commit()
    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    out = tmp_path / "sub" / f"{kind}.csv"
    assert (
        cli.main(["quality-sample", "--kind", kind, "--n", "2", "--seed", "1", "--out", str(out)])
        == 0
    )
    rows = _csv_rows(out.read_bytes())
    assert tuple(rows[0]) == headers
    assert len(rows) == 3
    assert "抽检表" in capsys.readouterr().out


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


def test_admin_quality_sample(client, session, monkeypatch):
    src = _source(session, "a")
    doc = _doc(session, src, 1)
    _detail(session, doc)
    _deadline(session, doc, "g1", date(2026, 10, 30), "企业申报截止时间为2026年10月30日 17:00")
    session.commit()
    audits = []
    monkeypatch.setattr(
        admin, "_audit", lambda request, action, **fields: audits.append((action, fields))
    )
    monkeypatch.setattr(admin.v1_api, "now", lambda: datetime(2026, 10, 4, 9, 0))

    path = "/api/admin/quality-sample?kind=notices&n=200&seed=1"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert audits == []

    response = client.get(path, headers=HEADERS)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == (
        'attachment; filename="youju-quality-notices-20261004-seed1.csv"'
    )
    assert response.headers["cache-control"] == "no-store"
    rows = _csv_rows(response.content)
    assert tuple(rows[0]) == NOTICE_HEADERS and len(rows) == 2
    assert rows[1][NOTICE_HEADERS.index("程序核对")] == "通过"
    assert audits == [("quality_sample", {"kind": "notices", "n": 200, "seed": 1, "count": 1})]

    compare = client.get("/api/admin/quality-sample?kind=compare&n=10", headers=HEADERS)
    assert compare.status_code == 200
    assert tuple(_csv_rows(compare.content)[0]) == COMPARE_HEADERS
    for bad in ("kind=notices&n=501", "kind=notices&n=0", "kind=other", "n=10"):
        assert client.get(f"/api/admin/quality-sample?{bad}", headers=HEADERS).status_code == 422


def test_portal_has_sample_downloads_without_token_in_url(client):
    page = client.get("/admin").text
    assert "抽检表：通知" in page and "抽检表：名单比对" in page
    urls = re.findall(r"/api/admin/quality-sample[^`'\"]*", page)
    assert urls and all("token" not in url.lower() for url in urls)
    assert "'X-Admin-Token':token" in page


def test_overall_says_only_source_checked_when_nothing_else_applies():
    from youju.quality import NOT_APPLICABLE, PASS, Check, _overall

    assert _overall([Check(PASS), Check(NOT_APPLICABLE), Check(NOT_APPLICABLE)]) == (
        "通过（只核对了来源：没有已核对的截止日期和字段）"
    )
    assert _overall([Check(PASS), Check(PASS), Check(NOT_APPLICABLE)]) == "通过"
