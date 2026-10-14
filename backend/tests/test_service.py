import logging
from datetime import UTC, date, datetime

from sqlalchemy import func, select

from tests.conftest import read_fixture_html
from youju.crawler.fetcher import FetchResult
from youju.crawler.listing import ListItem
from youju.crawler.service import check_source, crawl_sources, save_items
from youju.models import Document, Source, SourceCheck


class FakeFetcher:
    def __init__(self, responses: dict[str, list[FetchResult]]) -> None:
        self.responses = responses

    def fetch(self, url: str) -> FetchResult:
        return self.responses[url].pop(0)


def page(url: str, html: str) -> FetchResult:
    return FetchResult(url, "ok", final_url=url, http_status=200, text=html)


def make_source(session, url: str = "https://kw.beijing.gov.cn/zwgk/tzgg/") -> Source:
    source = Source(
        level="省级",
        province="北京",
        dept="科技",
        dept_full_name="北京市科学技术委员会",
        column_name="通知公告",
        url=url,
        fetch_mode="static",
        enabled=True,
        parser_config={},
    )
    session.add(source)
    session.commit()
    return source


LIST_V1 = """<ul>
<li><a href="/a/1.html">关于开展第一项申报工作的通知</a><span>2026-09-20</span></li>
<li><a href="/a/2.html">关于公布第二批认定名单的公示</a><span>2026-09-18</span></li>
</ul>"""
LIST_V2 = """<ul>
<li><a href="/a/3.html">关于征集第三项需求的通知公告</a><span>2026-09-25</span></li>
<li><a href="/a/1.html">关于开展第一项申报工作的通知</a><span>2026-09-20</span></li>
<li><a href="/a/2.html">关于公布第二批认定名单的公示</a><span>2026-09-18</span></li>
</ul>"""


def test_first_check_is_baseline_then_new_items(session) -> None:
    source = make_source(session)
    url = source.url
    fetcher = FakeFetcher({url: [page(url, LIST_V1), page(url, LIST_V2)]})

    first = check_source(session, source, fetcher)
    assert (first.status, first.items_found, first.items_new) == ("ok", 2, 2)
    assert first.is_baseline
    assert all(d.is_baseline for d in session.scalars(select(Document)))

    second = check_source(session, source, fetcher)
    assert (second.status, second.items_found, second.items_new) == ("ok", 3, 1)
    assert not second.is_baseline
    new_doc = session.scalar(select(Document).where(Document.is_baseline.is_(False)))
    assert new_doc.title == "关于征集第三项需求的通知公告"
    assert source.health == "ok"
    assert source.last_success_at is not None


def test_real_page_end_to_end(session) -> None:
    source = make_source(session)
    html = read_fixture_html("beijing_kw_tzgg")
    check = check_source(session, source, FakeFetcher({source.url: [page(source.url, html)]}))
    assert check.status == "ok"
    assert check.items_new == 20
    assert session.scalar(select(func.count(Document.id))) == 20


def test_failures_escalate_and_alert(session, caplog) -> None:
    source = make_source(session)
    err = FetchResult(source.url, "network_error", error="连接失败")
    fetcher = FakeFetcher({source.url: [err, err, err, page(source.url, LIST_V1)]})

    check_source(session, source, fetcher, failure_threshold=3)
    assert (source.health, source.consecutive_failures) == ("degraded", 1)
    check_source(session, source, fetcher, failure_threshold=3)
    with caplog.at_level(logging.WARNING, logger="youju.crawler.service"):
        check_source(session, source, fetcher, failure_threshold=3)
    assert (source.health, source.consecutive_failures) == ("failing", 3)
    assert "连续 3 次检查失败" in caplog.text

    # 恢复后清零；此前一直没成功过，所以这次发现的条目仍算基线
    check_source(session, source, fetcher, failure_threshold=3)
    assert (source.health, source.consecutive_failures) == ("ok", 0)
    assert all(d.is_baseline for d in session.scalars(select(Document)))
    assert session.scalar(select(func.count(SourceCheck.id))) == 4


def test_parse_empty_counts_as_failure(session) -> None:
    source = make_source(session)
    fetcher = FakeFetcher({source.url: [page(source.url, "<html><body>请开启脚本</body></html>")]})
    check = check_source(session, source, fetcher)
    assert check.status == "parse_empty"
    assert source.health == "degraded"
    assert "没有识别到列表条目" in source.last_error


def test_robots_disallowed_marks_blocked(session) -> None:
    source = make_source(session)
    denied = FetchResult(source.url, "robots_disallowed", error="robots.txt 不允许抓取该页面")
    check_source(session, source, FakeFetcher({source.url: [denied]}))
    assert source.health == "blocked"


def test_same_url_in_two_sources_stored_once(session) -> None:
    a = make_source(session, "https://kw.beijing.gov.cn/zwgk/tzgg/")
    b = make_source(session, "https://kw.beijing.gov.cn/zwgk/zcwj/")
    same = """<ul><li><a href="https://kw.beijing.gov.cn/art/1.html">同一份文件出现在两个栏目里</a><span>2026-09-20</span></li>
    <li><a href="https://kw.beijing.gov.cn/art/2.html">另一份文件的完整标题内容</a><span>2026-09-19</span></li></ul>"""
    fetcher = FakeFetcher({a.url: [page(a.url, same)], b.url: [page(b.url, same)]})
    assert check_source(session, a, fetcher).items_new == 2
    assert check_source(session, b, fetcher).items_new == 0
    assert session.scalar(select(func.count(Document.id))) == 2


def test_crawl_sources_runs_all(session_factory) -> None:
    with session_factory() as s:
        ids = [make_source(s, f"https://site{i}.gov.cn/list/").id for i in range(3)]
        urls = [s.get(Source, i).url for i in ids]
    fetcher = FakeFetcher({u: [page(u, LIST_V1)] for u in urls})
    results = crawl_sources(session_factory, ids, fetcher, max_workers=2)
    assert sorted(r.status for r in results) == ["ok", "ok", "ok"]


def test_crawl_summary_marks_baseline_runs(session_factory) -> None:
    with session_factory() as s:
        source_id = make_source(s, "https://site.gov.cn/list/").id
    url = "https://site.gov.cn/list/"
    fetcher = FakeFetcher({url: [page(url, LIST_V1), page(url, LIST_V2)]})
    first = crawl_sources(session_factory, [source_id], fetcher)[0]
    second = crawl_sources(session_factory, [source_id], fetcher)[0]
    assert (first.baseline, first.items_new) == (True, 2)
    assert (second.baseline, second.items_new) == (False, 1)


def test_save_items_handles_duplicate_urls_in_one_batch(session) -> None:
    source = make_source(session)
    item = ListItem(
        "同一条目出现两次的完整标题", "https://kw.beijing.gov.cn/art/9.html", date(2026, 9, 1)
    )
    now = datetime.now(UTC)
    assert save_items(session, source, [item, item], baseline=False, seen_at=now) == 1
    assert save_items(session, source, [item], baseline=False, seen_at=now) == 0


def test_url_variants_are_the_same_document(session) -> None:
    source = make_source(session)
    title = "关于开展第一项申报工作的通知"
    variants = [
        ListItem(title, "http://kw.beijing.gov.cn/a/1.html", None),
        ListItem(title, "https://kw.beijing.gov.cn/a/1.html;jsessionid=AAA", None),
        ListItem(title, "https://kw.beijing.gov.cn:443/a/1.html", None),
    ]
    now = datetime.now(UTC)
    assert save_items(session, source, variants, baseline=False, seen_at=now) == 1
    # 下次访问会话号变了，仍是同一份文件，不算新增
    later = [ListItem(title, "https://kw.beijing.gov.cn/a/1.html;jsessionid=BBB", None)]
    assert save_items(session, source, later, baseline=False, seen_at=now) == 0
    doc = session.scalar(select(Document))
    assert (doc.url_key, doc.url) == ("kw.beijing.gov.cn/a/1.html", variants[0].url)


def test_save_items_fixes_http_443_source_url(session) -> None:
    source = make_source(session)
    item = ListItem("关于开展新项目申报的通知", "http://kw.beijing.gov.cn:443/new.html", None)
    save_items(session, source, [item], baseline=False, seen_at=datetime.now(UTC))
    assert session.scalar(select(Document)).url == "https://kw.beijing.gov.cn/new.html"


def test_no_transaction_or_lock_is_held_while_fetching(session_factory) -> None:
    with session_factory() as s:
        source_id = make_source(s).id

    with session_factory() as s:

        class CheckingFetcher:
            def fetch(self, fetch_url: str) -> FetchResult:
                # 网络请求期间：本会话没有打开的事务，别的会话能立即锁住来源行
                assert not s.in_transaction()
                with session_factory() as other:
                    stmt = select(Source).where(Source.id == source_id).with_for_update(nowait=True)
                    assert other.scalars(stmt).first() is not None
                    other.rollback()
                return page(fetch_url, LIST_V1)

        check = check_source(s, s.get(Source, source_id), CheckingFetcher())
    assert (check.status, check.items_new, check.is_baseline) == ("ok", 2, True)


def test_baseline_is_decided_after_taking_the_lock(session_factory) -> None:
    """两次抓取重叠：后一次在前一次成功之前读到了来源，但不能再把自己当成第一次。"""
    with session_factory() as s:
        source_id = make_source(s).id
        url = s.get(Source, source_id).url
    with session_factory() as late, session_factory() as early:
        stale = late.get(Source, source_id)
        assert stale.last_success_at is None
        late.commit()
        check_source(early, early.get(Source, source_id), FakeFetcher({url: [page(url, LIST_V1)]}))
        check = check_source(late, stale, FakeFetcher({url: [page(url, LIST_V2)]}))
    assert (check.is_baseline, check.items_new) == (False, 1)
    with session_factory() as s:
        new_doc = s.scalar(select(Document).where(Document.is_baseline.is_(False)))
        assert new_doc.title == "关于征集第三项需求的通知公告"
