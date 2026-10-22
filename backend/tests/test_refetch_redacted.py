"""一次性重抓被旧的正文脱敏规则吞了字的详情（youju refetch-redacted，2026-10-20）。

库里的正文是按旧规则遮的（原文不存），这里用同一页面按旧规则遮出"库里的正文"，再看重抓能不能
找回被吞的字、原网页变了的不改、打不开的不改，写入后截止候选、AI 截止检查、"能不能 AI 解读"
跟着重算。"""

from datetime import UTC, date, datetime
from hashlib import sha256

import pytest
from sqlalchemy import func, select

from tests.test_detail_service import FakeFetcher, add_document
from youju import cli
from youju.crawler import detail_service
from youju.crawler.detail import parse_detail, redact_sensitive
from youju.crawler.fetcher import FetchResult
from youju.models import (
    AICheck,
    AIEligibility,
    ConditionList,
    DocumentDeadline,
    DocumentDetail,
    DocumentField,
    DocumentMerge,
)
from youju.refetch_redacted import old_redact, refetch_redacted

# 截止日期写在邮箱前面：旧规则整句遮掉；联系人这一行旧规则遮掉的是单位名
SWALLOWED = (
    "<html><body><div id='zoom'>"
    "<p>各有关单位：为做好2026年度专项资金项目申报工作，现将有关事项通知如下。</p>"
    "<p>请于2026年11月20日前将申报材料电子版发送至sbc_test@163.com邮箱，逾期不予受理。</p>"
    "<p>联系人：省工信厅中小企业处 张三 0571-87654321</p>"
    "<p>辽宁省工业和信息化厅</p><p>2026年10月9日</p>"
    "</div></body></html>"
)
# 旧规则本来就遮得对的
ALREADY_RIGHT = (
    "<html><body><div id='zoom'>"
    "<p>各有关单位：为做好2026年度专项资金项目申报工作，现将有关事项通知如下，请认真组织。</p>"
    "<p>联系人：张三，联系电话：0571-87654321。</p>"
    "</div></body></html>"
)
TITLE = "关于组织开展2026年度专项资金项目申报工作的通知"


def page(url: str, html: str) -> FetchResult:
    return FetchResult(url, "ok", final_url=url, http_status=200, text=html)


def stored_from(html: str, url: str) -> str:
    """当时入库的正文：同一页面按旧规则遮。"""
    return old_redact(parse_detail(html, url).raw_body)


def add_detail(session, doc, body: str, *, status: str = "ok") -> DocumentDetail:
    detail = DocumentDetail(
        document_id=doc.id,
        body_text=body,
        fetched_at=datetime(2026, 10, 1, tzinfo=UTC),
        http_status=200,
        status=status,
        attempt_count=1,
    )
    session.add(detail)
    session.commit()
    return detail


def body_of(session, doc_id: int) -> str:
    session.expire_all()
    return session.get(DocumentDetail, doc_id).body_text


@pytest.fixture
def world(session):
    swallowed = add_document(session, 1, title=TITLE)
    right = add_document(session, 2, source_id=swallowed.source_id, title=TITLE)
    changed = add_document(session, 3, source_id=swallowed.source_id, title=TITLE)
    gone = add_document(session, 4, source_id=swallowed.source_id, title=TITLE)
    add_detail(session, swallowed, stored_from(SWALLOWED, swallowed.url))
    add_detail(session, right, stored_from(ALREADY_RIGHT, right.url))
    # 原网页后来改过：库里是旧版
    add_detail(session, changed, stored_from(SWALLOWED, changed.url))
    add_detail(session, gone, stored_from(SWALLOWED, gone.url))
    # 不该选中的：正文里没有姓名、邮箱标记的；不是一级来源的；详情没抓成功的
    plain = add_document(session, 5, source_id=swallowed.source_id, title=TITLE)
    add_detail(session, plain, "各有关单位：请按要求组织申报。联系电话：[电话已脱敏]")
    other = add_document(session, 6, rights_level=2, title=TITLE)
    add_detail(session, other, stored_from(SWALLOWED, other.url))
    failed = add_document(session, 7, source_id=swallowed.source_id, title=TITLE)
    add_detail(session, failed, stored_from(SWALLOWED, failed.url), status="http_error")
    edited = SWALLOWED.replace("逾期不予受理", "逾期不再受理")
    fake = FakeFetcher(
        {
            swallowed.url: page(swallowed.url, SWALLOWED),
            right.url: page(right.url, ALREADY_RIGHT),
            changed.url: page(changed.url, edited),
            gone.url: FetchResult(gone.url, "http_error", http_status=404, error="HTTP 404"),
        }
    )
    docs = {"swallowed": swallowed, "right": right, "changed": changed, "gone": gone}
    return docs, fake


def test_old_rules_swallowed_the_deadline(world, session) -> None:
    docs, _ = world
    stored = body_of(session, docs["swallowed"].id)
    assert "11月20日" not in stored and "[邮箱已脱敏]邮箱" in stored
    assert "中小企业处" not in stored
    fresh = redact_sensitive(parse_detail(SWALLOWED, docs["swallowed"].url).raw_body)
    assert "请于2026年11月20日前将申报材料电子版发送至[邮箱已脱敏]邮箱" in fresh
    assert "联系人：省工信厅中小企业处 [姓名已脱敏] [电话已脱敏]" in fresh


def test_old_rules_copy_matches_what_was_stored() -> None:
    """模块里照抄的旧规则：和 9/27–10/20 入库时一样会吞字（拿它核对原网页变没变）。"""
    assert (
        old_redact("加盖公章，PDF电子版于7月2日前发送至abc@163.com邮箱")
        == "加盖公章，[邮箱已脱敏]邮箱"
    )
    assert old_redact("材料请于11月20日前寄至0571-87654321") == (
        "材料请于11月20[姓名已脱敏][电话已脱敏]"
    )
    assert old_redact("联系人：省工信厅汽车工业处 张三") == "联系人：[姓名已脱敏]处 张三"
    assert old_redact("账号：33050161613500000123") == "账号：33050161613500000123"


def test_preview_does_not_write(world, session) -> None:
    docs, fake = world
    before = {name: body_of(session, doc.id) for name, doc in docs.items()}
    report = refetch_redacted(session, limit=50, fetcher=fake)
    results = {r.document_id: r for r in report.results}
    assert [r.document_id for r in report.results] == [doc.id for doc in docs.values()]
    assert results[docs["swallowed"].id].result == "fixed"
    assert any("11月20日前" in piece for piece in results[docs["swallowed"].id].restored)
    assert results[docs["swallowed"].id].recheck_ai
    assert results[docs["right"].id].result == "same"
    changed = results[docs["changed"].id]
    assert changed.result == "changed" and 0.9 < changed.similarity < 1
    gone = results[docs["gone"].id]
    assert gone.result == "unavailable" and gone.reason == "HTTP 404"
    assert {name: body_of(session, doc.id) for name, doc in docs.items()} == before
    # 没选中的文件没去打开
    assert sorted(fake.calls) == sorted(doc.url for doc in docs.values())


def test_apply_replaces_body_and_recomputes(world, session) -> None:
    docs, fake = world
    swallowed = docs["swallowed"]
    session.add_all(
        [
            AICheck(
                document_id=swallowed.id,
                task=task,
                model="m",
                result=[],
                checked_at=datetime(2026, 10, 2, tzinfo=UTC),
            )
            for task in ("deadlines", "kind")
        ]
    )
    session.commit()
    stored = {name: body_of(session, doc.id) for name, doc in docs.items()}
    report = refetch_redacted(session, apply=True, limit=50, fetcher=fake)
    assert body_of(session, swallowed.id) == redact_sensitive(
        parse_detail(SWALLOWED, swallowed.url).raw_body
    )
    # 只改了能改的那份；抓取时间、状态没动
    for name in ("right", "changed", "gone"):
        assert body_of(session, docs[name].id) == stored[name]
    detail = session.get(DocumentDetail, swallowed.id)
    assert detail.fetched_at == datetime(2026, 10, 1, tzinfo=UTC) and detail.status == "ok"
    # 找回的截止日期当场成了待核对候选
    due = session.scalars(
        select(DocumentDeadline.candidate_date).where(DocumentDeadline.document_id == swallowed.id)
    ).all()
    assert date(2026, 11, 20) in due and report.new_candidates >= 1
    # AI 截止检查先清掉再算候选：新候选是"AI 还没查"，不是"AI 没找到"
    agrees = session.scalars(
        select(DocumentDeadline.ai_agrees).where(
            DocumentDeadline.document_id == swallowed.id,
            DocumentDeadline.candidate_date == date(2026, 11, 20),
        )
    ).all()
    assert agrees and all(a is None for a in agrees)
    # AI 截止检查清掉、下一轮重查（报的是实际清掉的条数）；类型检查留着
    assert report.ai_checks == 1
    tasks = session.scalars(select(AICheck.task).where(AICheck.document_id == swallowed.id)).all()
    assert tasks == ["kind"]
    # "能不能 AI 解读"当场重算
    assert report.eligibility_refreshed
    assert session.get(AIEligibility, swallowed.id) is not None
    # 再跑一遍：换过的这份已经是新规则遮的，不用改
    again = refetch_redacted(session, apply=True, limit=50, fetcher=fake)
    assert {r.document_id: r.result for r in again.results}[swallowed.id] == "same"


def test_apply_recomputes_eligibility_of_merge_master(world, session) -> None:
    """合并转载：主条目没有自己的正文时用组里的正文，副本换了正文，主条目的"能不能 AI 解读"也重算。"""
    docs, fake = world
    master = add_document(session, 10, source_id=docs["swallowed"].source_id, title=TITLE)
    session.add(
        DocumentMerge(
            duplicate_id=docs["swallowed"].id, canonical_id=master.id, match_key="k", active=True
        )
    )
    session.commit()
    report = refetch_redacted(session, apply=True, limit=50, fetcher=fake)
    assert report.eligibility_refreshed
    assert session.get(AIEligibility, master.id) is not None
    # 这一批"不用改"的也重算（中断后重跑能补上）；原网页变了、没打开的不在这次重算里
    assert session.get(AIEligibility, docs["right"].id) is not None
    assert session.get(AIEligibility, docs["changed"].id) is None
    assert session.get(AIEligibility, docs["gone"].id) is None


def test_ai_check_kept_when_only_unit_names_come_back(session) -> None:
    """只找回单位名、标签（没有日期、时刻）的：AI 截止检查不用重查，留着。"""
    html = (
        "<html><body><div id='zoom'>"
        "<p>各有关单位：为做好2026年度专项资金项目申报工作，现将有关事项通知如下，请认真组织。</p>"
        "<p>业务咨询：省工信厅中小企业处 张三 0571-87654321</p>"
        "</div></body></html>"
    )
    doc = add_document(session, 11, title=TITLE)
    add_detail(session, doc, stored_from(html, doc.url))
    session.add(
        AICheck(
            document_id=doc.id,
            task="deadlines",
            model="m",
            result=[],
            checked_at=datetime(2026, 10, 2, tzinfo=UTC),
        )
    )
    session.commit()
    fake = FakeFetcher({doc.url: page(doc.url, html)})
    preview = refetch_redacted(session, limit=50, fetcher=fake)
    assert [(r.result, r.recheck_ai) for r in preview.results] == [("fixed", False)]
    assert preview.ai_checks == 0
    report = refetch_redacted(session, apply=True, limit=50, fetcher=fake)
    assert [r.result for r in report.results] == ["fixed"]
    assert "中小企业处" in body_of(session, doc.id)
    assert session.scalars(select(AICheck.task).where(AICheck.document_id == doc.id)).all() == [
        "deadlines"
    ]


def test_apply_skips_bodies_updated_meanwhile(world, session) -> None:
    docs, fake = world
    swallowed = docs["swallowed"]
    _add_reviewed(session, swallowed.id)
    session.commit()
    original = fake.fetch

    def fetch(url):
        # 抓取期间别的任务（名单补抓附件等）已经把这份正文换了
        if url == swallowed.url:
            session.get(DocumentDetail, swallowed.id).body_text = "别的任务刚写入的正文"
            session.commit()
        return original(url)

    fake.fetch = fetch
    report = refetch_redacted(session, apply=True, limit=50, fetcher=fake, max_workers=1)
    result = {r.document_id: r for r in report.results}[swallowed.id]
    assert result.result == "changed" and "别的任务" in result.reason
    assert body_of(session, swallowed.id) == "别的任务刚写入的正文"
    # 份数按实际换了正文的报：这份没换，它的已核对字段、申报条件、AI 截止检查都不算
    assert (report.approved_fields, report.confirmed_conditions, report.ai_checks) == (0, 0, 0)
    assert session.scalar(select(func.count(AICheck.id))) == 1


def test_rerun_after_interruption_recomputes(world, session, monkeypatch) -> None:
    """正文换了、还没重算就中断：重跑时这份是"不用改"，也要补算截止候选和"能不能 AI 解读"。"""
    import youju.extract.service as extract_service

    docs, fake = world
    swallowed = docs["swallowed"]
    real = extract_service.extract_existing

    def interrupted(*args, **kwargs):
        raise RuntimeError("中断")

    monkeypatch.setattr(extract_service, "extract_existing", interrupted)
    with pytest.raises(RuntimeError):
        refetch_redacted(session, apply=True, limit=50, fetcher=fake)
    assert "11月20日前" in body_of(session, swallowed.id)
    due = select(DocumentDeadline.candidate_date).where(
        DocumentDeadline.document_id == swallowed.id
    )
    assert session.scalars(due).all() == []
    monkeypatch.setattr(extract_service, "extract_existing", real)
    report = refetch_redacted(session, apply=True, limit=50, fetcher=fake)
    assert {r.document_id: r.result for r in report.results}[swallowed.id] == "same"
    assert date(2026, 11, 20) in session.scalars(due).all()
    assert report.eligibility_refreshed
    assert session.get(AIEligibility, swallowed.id) is not None


def test_details_deleted_while_fetching(world, session, monkeypatch) -> None:
    """抓取期间详情被删了的：预览也标成已删除，不说"可以换成新正文"。"""
    docs, fake = world
    swallowed = docs["swallowed"]
    real = detail_service._fetch_outcomes

    def fetch_then_delete(selected, fetcher, workers):
        outcomes = real(selected, fetcher, workers)
        session.delete(session.get(DocumentDetail, swallowed.id))
        session.commit()
        return outcomes

    monkeypatch.setattr(detail_service, "_fetch_outcomes", fetch_then_delete)
    report = refetch_redacted(session, limit=50, fetcher=fake)
    result = {r.document_id: r for r in report.results}[swallowed.id]
    assert result.result == "missing" and result.reason == "详情已删除"


def test_only_given_ids(world, session) -> None:
    docs, fake = world
    report = refetch_redacted(
        session, ids=[docs["gone"].id, docs["swallowed"].id], limit=2, fetcher=fake
    )
    assert [r.document_id for r in report.results] == [docs["swallowed"].id, docs["gone"].id]
    assert sorted(fake.calls) == sorted([docs["swallowed"].url, docs["gone"].url])


def test_limit_and_after_id(world, session) -> None:
    docs, fake = world
    with pytest.raises(ValueError, match="limit"):
        refetch_redacted(session, limit=0, fetcher=fake)
    first = refetch_redacted(session, limit=2, fetcher=fake)
    assert [r.document_id for r in first.results] == [docs["swallowed"].id, docs["right"].id]
    rest = refetch_redacted(session, limit=50, after_id=docs["right"].id, fetcher=fake)
    assert [r.document_id for r in rest.results] == [docs["changed"].id, docs["gone"].id]


def test_pdf_bodies_are_checked_the_same_way(session, monkeypatch) -> None:
    """正文取自 PDF 的（详情链接就是 PDF）：同样拿 PDF 原文按旧规则核对。"""
    raw = (
        "各有关单位：为做好2026年度专项资金项目申报工作，现将有关事项通知如下。\n"
        "请于2026年11月20日前将申报材料电子版发送至sbc_test@163.com邮箱。"
    )
    monkeypatch.setattr(detail_service, "extract_pdf_text", lambda content: (raw, None))
    doc = add_document(session, 8, title=TITLE)
    doc.url = doc.url.replace(".html", ".pdf")
    session.commit()
    add_detail(session, doc, old_redact(raw))
    pdf = FetchResult(
        doc.url,
        "ok",
        final_url=doc.url,
        http_status=200,
        content=b"%PDF-1.4",
        content_type="application/pdf",
    )
    report = refetch_redacted(session, apply=True, limit=50, fetcher=FakeFetcher({doc.url: pdf}))
    assert [r.result for r in report.results] == ["fixed"]
    assert body_of(session, doc.id) == redact_sensitive(raw)


def test_page_with_only_a_pdf(session, monkeypatch) -> None:
    """页面没有正文、只有一个 PDF 附件的（补详情另抓这个 PDF 当正文）：同样拿 PDF 原文核对。"""
    raw = "各有关单位：请于2026年11月20日前将申报材料电子版发送至sbc_test@163.com邮箱。"
    monkeypatch.setattr(detail_service, "extract_pdf_text", lambda content: (raw, None))
    doc = add_document(session, 9, title=TITLE)
    add_detail(session, doc, old_redact(raw))
    pdf_url = "https://gxt.ln.gov.cn/files/notice.pdf"
    html = f"<html><body><div id='zoom'><p><a href='{pdf_url}'>通知全文.pdf</a></p></div></body></html>"
    pdf = FetchResult(
        pdf_url,
        "ok",
        final_url=pdf_url,
        http_status=200,
        content=b"%PDF-1.4",
        content_type="application/pdf",
    )
    fake = FakeFetcher({doc.url: page(doc.url, html), pdf_url: pdf})
    report = refetch_redacted(session, apply=True, limit=50, fetcher=fake)
    assert [r.result for r in report.results] == ["fixed"]
    assert body_of(session, doc.id) == redact_sensitive(raw)


def _add_reviewed(session, doc_id: int) -> None:
    """给这份文件加上已核对的字段、确认过的申报条件清单（都按现在库里的正文）和一条 AI 截止检查。"""
    digest = sha256(body_of(session, doc_id).encode()).hexdigest()
    now = datetime(2026, 10, 3, tzinfo=UTC)
    session.add(
        DocumentField(
            document_id=doc_id,
            field="support",
            value="资金补助",
            evidence="资金补助",
            evidence_offset=0,
            body_hash=digest,
            status="approved",
            reviewed_by="核对员",
            reviewed_at=now,
        )
    )
    session.add(
        ConditionList(
            document_id=doc_id,
            body_hash=digest,
            status="confirmed",
            model="m",
            prompt_version="v1",
            extracted_at=now,
            confirmed_by="核对员",
            confirmed_at=now,
        )
    )
    session.add(AICheck(document_id=doc_id, task="deadlines", model="m", result=[], checked_at=now))


def test_preview_counts_what_needs_review_again(world, session) -> None:
    docs, fake = world
    _add_reviewed(session, docs["swallowed"].id)
    now = datetime(2026, 10, 3, tzinfo=UTC)
    # 旧正文的清单、别的文件的不算
    session.add(
        ConditionList(
            document_id=docs["right"].id,
            body_hash=sha256(body_of(session, docs["right"].id).encode()).hexdigest(),
            status="confirmed",
            model="m",
            prompt_version="v1",
            extracted_at=now,
            confirmed_by="核对员",
            confirmed_at=now,
        )
    )
    session.commit()
    report = refetch_redacted(session, limit=50, fetcher=fake)
    assert (report.approved_fields, report.confirmed_conditions, report.ai_checks) == (1, 1, 1)
    # 预览不清 AI 检查
    assert session.scalar(select(func.count(AICheck.id))) == 1


def test_refetch_redacted_command(world, session, monkeypatch, capsys) -> None:
    docs, fake = world

    class Polite:
        def __enter__(self):
            return fake

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(detail_service, "_polite_fetcher", Polite)
    # 只统计，不打开网页
    assert cli.main(["refetch-redacted", "--count"]) == 0
    out = capsys.readouterr().out
    assert "要重新打开看的（含[邮箱已脱敏]或[姓名已脱敏]）4 份" in out
    assert (
        "refetch_redacted_count bodies=5 email=3 name_phone=3 name=4 candidates=4 "
        f"first_id={docs['swallowed'].id} last_id={docs['gone'].id}"
    ) in out
    assert fake.calls == []

    _add_reviewed(session, docs["swallowed"].id)
    session.commit()
    assert cli.main(["refetch-redacted"]) == 0
    out = capsys.readouterr().out
    assert f"#{docs['swallowed'].id} 可以换成新正文" in out
    assert "11月20日前" in out
    assert f"#{docs['changed'].id} 原网页变了，没改（相似度" in out
    assert f"#{docs['gone'].id} 没打开，没改（HTTP 404）" in out
    assert (
        "预览（未写入）：检查 4 份，可以换成新正文 1 份，不用改 1 份，原网页变了 1 份，"
        "没打开 1 份，已删除 0 份"
    ) in out
    assert "确认无误后加 --apply 写入" in out
    assert (
        "refetch_redacted mode=preview checked=4 fixed=1 same=1 changed=1 unavailable=1 "
        f"missing=0 fields=1 conditions=1 ai_checks=1 last_id={docs['gone'].id}"
    ) in out
    # 没打开的分批时会被 --after-id 跳过，最后一行给出重试用的编号
    assert out.rstrip().endswith(f"refetch_redacted_retry ids={docs['gone'].id}")

    assert cli.main(["refetch-redacted", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "已写入：检查 4 份，已换成新正文 1 份" in out
    assert "新增待核对截止候选" in out
    assert (
        "refetch_redacted mode=apply checked=4 fixed=1 same=1 changed=1 unavailable=1 missing=0 "
        f"fields=1 conditions=1 ai_checks=1 candidates=1 eligibility=1 last_id={docs['gone'].id}"
    ) in out
    assert "11月20日前" in body_of(session, docs["swallowed"].id)

    assert cli.main(["refetch-redacted", "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert f"已到 --limit 1 份，后面可能还有：加 --after-id {docs['swallowed'].id}" in out

    # 只重试没打开的那份
    assert cli.main(["refetch-redacted", "--ids", str(docs["gone"].id)]) == 0
    out = capsys.readouterr().out
    assert "refetch_redacted mode=preview checked=1 " in out and "--after-id" not in out
    with pytest.raises(SystemExit):
        cli.main(["refetch-redacted", "--ids", "1", "--after-id", "1"])
    with pytest.raises(SystemExit):
        cli.main(["refetch-redacted", "--ids", "a,b"])


def test_old_rules_copy_does_not_hang_on_long_whitespace() -> None:
    import time

    started = time.perf_counter()
    for label in ("账号", "联系人"):
        old_redact(label + " " * 100_000 + "x")
    assert time.perf_counter() - started < 2
