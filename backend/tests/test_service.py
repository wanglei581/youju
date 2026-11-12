import logging
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select

from tests.conftest import read_fixture_html
from youju.crawler.fetcher import FetchResult
from youju.crawler.listing import ListItem
from youju.crawler.service import check_source, crawl_sources, save_items
from youju.models import Document, Source, SourceCheck


class FakeFetcher:
    def __init__(self, responses: dict[str, list[FetchResult]]) -> None:
        self.responses = responses

    def fetch(self, url: str, same_address_only: bool = False) -> FetchResult:
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
            def fetch(self, fetch_url: str, same_address_only: bool = False) -> FetchResult:
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


# 复检、抽查合格、打开时才写上的"经接口读"标记（D-128）：只有带它的来源，正式抓取才走接口
API_OPEN = {"list_api_open": "大汉jpaas"}


def make_api_source(session, url: str) -> Source:
    source = make_source(session, url)
    source.parser_config = dict(API_OPEN)
    session.commit()
    return source


JPAAS_PAGE = """<html><body><div class="col_right"><ul>
<script id="u1" src="/cms_files/default/script/AuthorizedRead/unitbuild.js" webId="3387"
 parseType="bulidstatic" url="/api-gateway/jpaas-publish-server/front/page/build/unit"
 queryData="{'parseType':'bulidstatic','webId':'3387','tplSetId':'T1','pageType':'column','tagId':'当前栏目list','editType':'null','pageId':'1229225203'}"></script>
</ul></div></body></html>"""
JPAAS_JSON = (
    '{"roles":null,"permissions":null,"success":true,"message":"生成成功","code":"200","traceId":"t","messageType":2,"data":{"html":"<li><a href=\\"/col/col1229225203/art/2026/art_1.html\\">'
    "关于开展2026年度第一批项目申报的通知</a><span>2026-11-05</span></li>"
    '<li><a href=\\"/col/col1229225203/art/2026/art_2.html\\">关于公布第二批认定名单的公示</a>'
    '<span>2026-11-04</span></li>"}}'
)


def test_hanweb_jpaas_list_api_is_used_when_page_has_no_items(session) -> None:
    url = "https://kjt.zj.gov.cn/col/col1229225203/index.html"
    source = make_api_source(session, url)
    api = (
        "https://kjt.zj.gov.cn/api-gateway/jpaas-publish-server/front/page/build/unit?parseType=bulidstatic"
        "&webId=3387&tplSetId=T1&pageType=column&tagId=%E5%BD%93%E5%89%8D%E6%A0%8F%E7%9B%AElist"
        "&editType=null&pageId=1229225203"
    )
    fetcher = FakeFetcher({url: [page(url, JPAAS_PAGE)], api: [page(api, JPAAS_JSON)]})
    check = check_source(session, source, fetcher)
    assert (check.status, check.items_found) == ("ok", 2)
    urls = set(session.scalars(select(Document.url).where(Document.source_id == source.id)))
    assert urls == {
        "https://kjt.zj.gov.cn/col/col1229225203/art/2026/art_1.html",
        "https://kjt.zj.gov.cn/col/col1229225203/art/2026/art_2.html",
    }


def test_first_read_via_list_api_is_baseline_for_existing_source(session) -> None:
    """以前只认出零星条目的来源，第一次经列表接口读出整页：旧通知按基线记，不算新增。"""
    url = "https://kjt.zj.gov.cn/col/col1229225203/index.html"
    source = make_api_source(session, url)
    source.last_success_at = datetime(2026, 11, 1, tzinfo=UTC)
    session.commit()
    api = (
        "https://kjt.zj.gov.cn/api-gateway/jpaas-publish-server/front/page/build/unit?parseType=bulidstatic"
        "&webId=3387&tplSetId=T1&pageType=column&tagId=%E5%BD%93%E5%89%8D%E6%A0%8F%E7%9B%AElist"
        "&editType=null&pageId=1229225203"
    )
    fetcher = FakeFetcher({url: [page(url, JPAAS_PAGE)], api: [page(api, JPAAS_JSON)]})
    check = check_source(session, source, fetcher)
    assert (check.status, check.items_found, check.is_baseline) == ("ok", 2, True)
    session.refresh(source)
    assert source.parser_config.get("list_api") == "大汉jpaas"


def test_blocked_list_api_is_recorded_not_parse_empty(session) -> None:
    url = "https://kjt.zj.gov.cn/col/col1229225203/index.html"
    source = make_api_source(session, url)
    api = (
        "https://kjt.zj.gov.cn/api-gateway/jpaas-publish-server/front/page/build/unit?parseType=bulidstatic"
        "&webId=3387&tplSetId=T1&pageType=column&tagId=%E5%BD%93%E5%89%8D%E6%A0%8F%E7%9B%AElist"
        "&editType=null&pageId=1229225203"
    )
    denied = FetchResult(
        api, "robots_disallowed", final_url=api, error="robots.txt 不允许抓取该页面"
    )
    fetcher = FakeFetcher({url: [page(url, JPAAS_PAGE)], api: [denied]})
    check = check_source(session, source, fetcher)
    assert check.status == "robots_disallowed"
    assert "列表接口" in check.error
    session.refresh(source)
    assert source.health == "blocked"


JPAAS_API = (
    "https://kjt.zj.gov.cn/api-gateway/jpaas-publish-server/front/page/build/unit?parseType=bulidstatic"
    "&webId=3387&tplSetId=T1&pageType=column&tagId=%E5%BD%93%E5%89%8D%E6%A0%8F%E7%9B%AElist"
    "&editType=null&pageId=1229225203"
)


def _check_with_api(session, api_result: FetchResult):
    url = "https://kjt.zj.gov.cn/col/col1229225203/index.html"
    source = make_api_source(session, url)
    fetcher = FakeFetcher({url: [page(url, JPAAS_PAGE)], JPAAS_API: [api_result]})
    check = check_source(session, source, fetcher)
    session.refresh(source)
    docs = session.scalar(select(func.count()).select_from(Document))
    return check, source, docs


def test_list_api_not_success_is_refused_and_crawling_stops(session) -> None:
    """读取护栏（D-128）：接口返回不是成功 → 记不能抓（和 robots 禁止一样停抓），不读。"""
    body = '{"success":false,"code":"200","message":"","data":{}}'
    check, source, docs = _check_with_api(session, page(JPAAS_API, body))
    assert (check.status, check.items_found, docs) == ("list_api_refused", 0, 0)
    assert "列表接口" in check.error and "不是成功" in check.error
    assert source.health == "blocked"
    note = source.parser_config["list_api_refused"]
    assert (note["url"], note["reason"]) == (JPAAS_API, check.error)


def test_refused_list_api_is_not_requested_again(session) -> None:
    """例行抓取不跳过 blocked 的来源：拦下过一次就记在来源上，以后不再请求这个接口。"""
    url = "https://kjt.zj.gov.cn/col/col1229225203/index.html"
    source = make_api_source(session, url)
    body = '{"success":false,"code":"200","message":"","data":{}}'
    # 第二次只给栏目页：再请求列表接口就会出错
    fetcher = FakeFetcher(
        {url: [page(url, JPAAS_PAGE), page(url, JPAAS_PAGE)], JPAAS_API: [page(JPAAS_API, body)]}
    )
    first = check_source(session, source, fetcher)
    second = check_source(session, source, fetcher)
    assert (first.status, second.status) == ("list_api_refused", "list_api_refused")
    assert "不再请求" in second.error
    session.refresh(source)
    assert source.health == "blocked"


def test_refused_list_api_drops_stray_page_items(session) -> None:
    """栏目页上零星认出一两条、列表在接口里且接口被拦：照样记不能抓，零星条目也不收。"""
    url = "https://kjt.zj.gov.cn/col/col1229225203/index.html"
    source = make_api_source(session, url)
    stray = JPAAS_PAGE.replace(
        "</ul>",
        '<li><a href="/art/9.html">关于开展专项申报工作的通知</a><span>2026-11-01</span></li></ul>',
    )
    body = JPAAS_JSON.replace('"roles":null', '"roles":["dept"]')
    fetcher = FakeFetcher({url: [page(url, stray)], JPAAS_API: [page(JPAAS_API, body)]})
    check = check_source(session, source, fetcher)
    assert (check.status, check.items_found) == ("list_api_refused", 0)
    assert session.scalar(select(func.count()).select_from(Document)) == 0


def test_list_api_with_permission_fields_is_not_read(session) -> None:
    body = JPAAS_JSON.replace('"roles":null', '"roles":["dept"]')
    check, source, docs = _check_with_api(session, page(JPAAS_API, body))
    assert (check.status, docs) == ("list_api_refused", 0)
    assert "roles" in check.error
    assert source.health == "blocked"


def test_list_api_redirected_to_login_is_refused(session) -> None:
    login = "https://kjt.zj.gov.cn/jpaas-jsam-server/login?redirect=1"
    form = '<form><input type="password" name="p"></form>'
    got = FetchResult(JPAAS_API, "ok", final_url=login, http_status=200, text=form)
    check, source, docs = _check_with_api(session, got)
    assert (check.status, docs) == ("list_api_refused", 0)
    assert "登录" in check.error and source.health == "blocked"


def test_list_api_refusing_http_status_stops_crawling(session) -> None:
    got = FetchResult(
        JPAAS_API, "http_error", final_url=JPAAS_API, http_status=403, error="HTTP 403"
    )
    check, source, _ = _check_with_api(session, got)
    assert (check.status, check.http_status) == ("list_api_refused", 403)
    assert source.health == "blocked"


def test_list_api_one_off_failure_is_retried_later(session) -> None:
    """网络不通、5xx 只是这次没取到：照常记失败，不记不能抓。"""
    got = FetchResult(
        JPAAS_API, "http_error", final_url=JPAAS_API, http_status=502, error="HTTP 502"
    )
    check, source, _ = _check_with_api(session, got)
    assert check.status == "http_error"
    assert source.health == "degraded"


def _refused_note(url: str, days_ago: int) -> dict:
    at = datetime.now(UTC) - timedelta(days=days_ago)
    return {
        "list_api_refused": {
            "url": url,
            "reason": "列表接口（大汉jpaas）：旧原因",
            "at": at.isoformat(),
        }
    }


def test_refusal_is_rechecked_when_page_declares_another_api_or_after_30_days(session) -> None:
    """页面换了接口、或拦下已过 30 天：再请求一次；没被拦就照常读，去掉旧记录。"""
    url = "https://kjt.zj.gov.cn/col/col1229225203/index.html"
    for note in (_refused_note(JPAAS_API + "&old=1", 1), _refused_note(JPAAS_API, 40)):
        session.query(Document).delete()
        session.query(SourceCheck).delete()
        session.query(Source).delete()
        session.commit()
        source = make_api_source(session, url)
        source.parser_config = {**API_OPEN, **note}
        session.commit()
        fetcher = FakeFetcher(
            {url: [page(url, JPAAS_PAGE)], JPAAS_API: [page(JPAAS_API, JPAAS_JSON)]}
        )
        check = check_source(session, source, fetcher)
        assert (check.status, check.items_found) == ("ok", 2)
        session.refresh(source)
        assert "list_api_refused" not in source.parser_config


def test_unrecognized_list_api_reply_is_not_read(session) -> None:
    """认得出才读：接口回认不出的网页，不读，记不能抓（30 天后再查）。"""
    check, source, docs = _check_with_api(
        session, page(JPAAS_API, "<html><body><p>欢迎访问</p></body></html>")
    )
    assert (check.status, docs) == ("list_api_refused", 0)
    assert "认不出" in check.error
    assert source.health == "blocked"


def test_maintenance_list_api_reply_is_retried_not_read(session) -> None:
    """#475 后续：接口回维护页、空白页，算这次没取到（下次再试），照样不读，不记 30 天的不能抓。"""
    check, source, docs = _check_with_api(session, page(JPAAS_API, "<html>系统维护中</html>"))
    assert (check.status, docs) == ("parse_empty", 0)
    assert source.health != "blocked"
    assert "list_api_refused" not in (source.parser_config or {})


def test_robots_denied_list_api_drops_stray_page_items(session) -> None:
    url = "https://kjt.zj.gov.cn/col/col1229225203/index.html"
    source = make_api_source(session, url)
    stray = JPAAS_PAGE.replace(
        "</ul>",
        '<li><a href="/art/9.html">关于开展专项申报工作的通知</a><span>2026-11-01</span></li></ul>',
    )
    denied = FetchResult(
        JPAAS_API, "robots_disallowed", final_url=JPAAS_API, error="robots.txt 不允许"
    )
    fetcher = FakeFetcher({url: [page(url, stray)], JPAAS_API: [denied]})
    check = check_source(session, source, fetcher)
    assert (check.status, check.items_found) == ("robots_disallowed", 0)
    session.refresh(source)
    assert source.health == "blocked"


class RecordingFetcher(FakeFetcher):
    def __init__(self, responses: dict[str, list[FetchResult]]) -> None:
        super().__init__(responses)
        self.requested: list[str] = []

    def fetch(self, url: str, same_address_only: bool = False) -> FetchResult:
        self.requested.append(url)
        return super().fetch(url, same_address_only)


def test_only_the_page_and_its_declared_api_are_requested(session) -> None:
    """D-128 护栏 ①：除了栏目页和页面写明的那个接口地址，不发别的请求。"""
    url = "https://kjt.zj.gov.cn/col/col1229225203/index.html"
    source = make_api_source(session, url)
    fetcher = RecordingFetcher(
        {url: [page(url, JPAAS_PAGE)], JPAAS_API: [page(JPAAS_API, JPAAS_JSON)]}
    )
    check = check_source(session, source, fetcher)
    assert check.status == "ok"
    assert fetcher.requested == [url, JPAAS_API]


def test_recheck_keeps_refusal_on_one_off_failure(session) -> None:
    """过了 30 天再请求，这次只是 5xx：旧的拦下记录留着，不当成已经能读。"""
    url = "https://kjt.zj.gov.cn/col/col1229225203/index.html"
    source = make_api_source(session, url)
    source.parser_config = {**API_OPEN, **_refused_note(JPAAS_API, 40)}
    session.commit()
    failed = FetchResult(
        JPAAS_API, "http_error", final_url=JPAAS_API, http_status=502, error="HTTP 502"
    )
    fetcher = FakeFetcher({url: [page(url, JPAAS_PAGE)], JPAAS_API: [failed]})
    check = check_source(session, source, fetcher)
    assert check.status == "http_error"
    session.refresh(source)
    assert source.parser_config["list_api_refused"]["url"] == JPAAS_API


def test_one_off_list_api_failure_is_not_hidden_by_stray_page_items(session) -> None:
    """接口这次 502、栏目页零星认出一条：照实记失败，不把这一条当成功入库。"""
    url = "https://kjt.zj.gov.cn/col/col1229225203/index.html"
    source = make_api_source(session, url)
    stray = JPAAS_PAGE.replace(
        "</ul>",
        '<li><a href="/art/9.html">关于开展专项申报工作的通知</a><span>2026-11-01</span></li></ul>',
    )
    failed = FetchResult(
        JPAAS_API, "http_error", final_url=JPAAS_API, http_status=502, error="HTTP 502"
    )
    fetcher = FakeFetcher({url: [page(url, stray)], JPAAS_API: [failed]})
    check = check_source(session, source, fetcher)
    assert (check.status, check.items_found) == ("http_error", 0)
    assert "列表接口" in check.error
    session.refresh(source)
    assert source.health == "degraded"


def test_unreadable_list_api_reply_drops_stray_page_items(session) -> None:
    """接口回 200 但读不出列表（空白、不是 JSON）：栏目页零星认出的一条也不收，记没识别到条目。"""
    url = "https://kjt.zj.gov.cn/col/col1229225203/index.html"
    stray = JPAAS_PAGE.replace(
        "</ul>",
        '<li><a href="/art/9.html">关于开展专项申报工作的通知</a><span>2026-11-01</span></li></ul>',
    )
    empty = '{"roles":null,"permissions":null,"success":true,"message":"生成成功","code":"200","traceId":"t","messageType":2,"data":{"html":""}}'
    # 空白页、维护页算这次没取到（#475 后续），认不出的网页记不能抓：栏目页上零星的一条都不收
    cases = [
        ("", "parse_empty"),
        ("<html>系统维护中</html>", "parse_empty"),
        ("<html><body><p>欢迎访问</p></body></html>", "list_api_refused"),
        (empty, "parse_empty"),
    ]
    for body, want in cases:
        session.query(Document).delete()
        session.query(SourceCheck).delete()
        session.query(Source).delete()
        session.commit()
        source = make_api_source(session, url)
        fetcher = FakeFetcher({url: [page(url, stray)], JPAAS_API: [page(JPAAS_API, body)]})
        check = check_source(session, source, fetcher)
        assert (check.status, check.items_found) == (want, 0)
        assert session.scalar(select(func.count()).select_from(Document)) == 0


def test_refusal_note_is_merged_into_the_latest_config(session, session_factory) -> None:
    """拦下记录合到库里最新的配置上：检查期间别处改过的配置项不会被旧副本冲掉。"""
    url = "https://kjt.zj.gov.cn/col/col1229225203/index.html"
    source = make_api_source(session, url)
    body = '{"success":false,"code":"200","message":"","data":{}}'

    class EditingFetcher(FakeFetcher):
        def fetch(self, fetch_url: str, same_address_only: bool = False) -> FetchResult:
            if fetch_url == JPAAS_API:
                # 检查进行中，另一处给这个来源写了别的配置
                with session_factory() as other:
                    row = other.get(Source, source.id)
                    row.parser_config = {**(row.parser_config or {}), "note": "别处写的"}
                    other.commit()
            return super().fetch(fetch_url, same_address_only)

    fetcher = EditingFetcher({url: [page(url, JPAAS_PAGE)], JPAAS_API: [page(JPAAS_API, body)]})
    check = check_source(session, source, fetcher)
    assert check.status == "list_api_refused"
    session.refresh(source)
    assert source.parser_config["note"] == "别处写的"
    assert source.parser_config["list_api_refused"]["url"] == JPAAS_API


def test_dropping_old_refusal_keeps_a_newer_one_written_meanwhile(session, session_factory) -> None:
    """过期的旧记录本次去掉时，库里若已换成别的检查刚记下的新记录，就不动它。"""
    url = "https://kjt.zj.gov.cn/col/col1229225203/index.html"
    source = make_api_source(session, url)
    source.parser_config = {**API_OPEN, **_refused_note(JPAAS_API, 40)}
    session.commit()
    newer = {**API_OPEN, **_refused_note(JPAAS_API, 0)}

    class RacingFetcher(FakeFetcher):
        def fetch(self, fetch_url: str, same_address_only: bool = False) -> FetchResult:
            if fetch_url == JPAAS_API:
                with session_factory() as other:  # 另一次检查刚记下新的拦下记录
                    row = other.get(Source, source.id)
                    row.parser_config = newer
                    other.commit()
            return super().fetch(fetch_url, same_address_only)

    fetcher = RacingFetcher(
        {url: [page(url, JPAAS_PAGE)], JPAAS_API: [page(JPAAS_API, JPAAS_JSON)]}
    )
    check = check_source(session, source, fetcher)
    assert check.status == "ok"
    session.refresh(source)
    assert source.parser_config["list_api_refused"] == newer["list_api_refused"]


def test_list_api_redirect_is_recorded_as_not_read_this_time(session) -> None:
    """接口要跳转（哪怕只是 http 升 https）：不跟、不读，记这次没读出，不记不能抓。"""
    target = JPAAS_API.replace("https://", "http://")
    got = FetchResult(JPAAS_API, "redirect", final_url=JPAAS_API + "&x=1", http_status=302)
    check, source, docs = _check_with_api(session, got)
    assert (check.status, docs) == ("list_api_refused", 0)  # 跳到别的地址：不能抓
    session.query(Document).delete()
    session.query(SourceCheck).delete()
    session.query(Source).delete()
    session.commit()
    scheme_only = FetchResult(JPAAS_API, "redirect", final_url=JPAAS_API, http_status=301)
    check, source, _ = _check_with_api(session, scheme_only)
    assert check.status == "redirect" and "不跟" in check.error
    assert source.health == "degraded"
    assert target  # 只用来说明 http/https 两种写法


def test_unmarked_source_never_calls_the_list_api(session) -> None:
    """D-128：没有"经接口读"标记的已启用来源，条目少于 3 条、页面又写着接口时也不调接口、不读；
    记"读不出"并写明待复检，不悄悄记成 0 条。"""
    url = "https://kjt.zj.gov.cn/col/col1229225203/index.html"
    source = make_source(session, url)  # 没有标记
    stray = JPAAS_PAGE.replace(
        "</ul>",
        '<li><a href="/art/9.html">关于开展专项申报工作的通知</a><span>2026-11-01</span></li></ul>',
    )
    fetcher = RecordingFetcher({url: [page(url, stray)]})  # 只给栏目页：调接口就会出错
    check = check_source(session, source, fetcher)
    assert fetcher.requested == [url]
    assert (check.status, check.items_found) == ("parse_empty", 0)
    assert "待复检" in check.error
    session.refresh(source)
    assert source.health == "degraded"
    assert session.scalar(select(func.count()).select_from(Document)) == 0


def test_marked_source_reads_the_list_api_through_the_guard(session) -> None:
    url = "https://kjt.zj.gov.cn/col/col1229225203/index.html"
    source = make_api_source(session, url)
    fetcher = RecordingFetcher(
        {url: [page(url, JPAAS_PAGE)], JPAAS_API: [page(JPAAS_API, JPAAS_JSON)]}
    )
    check = check_source(session, source, fetcher)
    assert (check.status, check.items_found) == ("ok", 2)
    assert fetcher.requested == [url, JPAAS_API]
    # 标记是别的适配器名（或写错）的，照样不走接口
    other = make_source(session, "https://kjt.zj.gov.cn/col/col2/index.html")
    other.parser_config = {"list_api_open": "别的平台"}
    session.commit()
    other_url = other.url
    f2 = RecordingFetcher({other_url: [page(other_url, JPAAS_PAGE)]})
    assert check_source(session, other, f2).status == "parse_empty" and f2.requested == [other_url]


def test_gkmlpt_requires_matching_marker_and_ignores_page_items(session):
    from pathlib import Path

    folder = Path(__file__).parent / "fixtures/gkmlpt"
    html = (folder / "dept_page.html").read_text()
    html = html.replace("</body>", LIST_V2 + "</body>")
    from youju.crawler.listing import extract_items

    assert (
        len(extract_items(html, "https://www.sg.gov.cn/fgj/gkmlpt/index", today=date(2026, 11, 12)))
        >= 3
    )
    payload = (folder / "api_notice.json").read_text()
    page_url = "https://www.sg.gov.cn/fgj/gkmlpt/index"
    api_url = "https://www.sg.gov.cn/fgj/gkmlpt/api/all/8?page=1&sid=751012"
    url = page_url + "#xxgk-gkmlpt-8"
    source = make_source(session, url)
    for marker in (None, "大汉jpaas", "数字广东gkmlpt"):
        source.parser_config = {"list_api_open": marker} if marker else {}
        session.commit()
        responses = {url: [page(page_url, html)]}
        if marker == "数字广东gkmlpt":
            responses[api_url] = [page(api_url, payload)]
        fetcher = RecordingFetcher(responses)
        check = check_source(session, source, fetcher, today=date(2026, 11, 12))
        if marker == "数字广东gkmlpt":
            assert (check.status, check.items_found) == ("ok", 5)
            assert fetcher.requested == [url, api_url]
        else:
            assert check.status == "parse_empty" and "待复检" in check.error
            assert fetcher.requested == [url]


def _gkmlpt_fixture():
    from pathlib import Path

    folder = Path(__file__).parent / "fixtures/gkmlpt"
    page_url = "https://www.sg.gov.cn/fgj/gkmlpt/index"
    return (
        (folder / "dept_page.html").read_text(),
        (folder / "api_notice.json").read_text(),
        page_url,
        "https://www.sg.gov.cn/fgj/gkmlpt/api/all/8?page=1&sid=751012",
        page_url + "#xxgk-gkmlpt-8",
    )


def test_gkmlpt_page_without_the_api_is_not_requested(session):
    # 标了"经接口读"，但部门页改版、认不出接口：不请求接口，记读不出并写明原因
    from youju.crawler.service import GKMLPT_UNRECOGNIZED_MESSAGE

    _, _, page_url, _, url = _gkmlpt_fixture()
    source = make_source(session, url)
    source.parser_config = {"list_api_open": "数字广东gkmlpt"}
    session.commit()
    fetcher = RecordingFetcher({url: [page(page_url, "<html><body>改版了</body></html>")]})
    check = check_source(session, source, fetcher, today=date(2026, 11, 12))
    assert (check.status, check.error) == ("parse_empty", GKMLPT_UNRECOGNIZED_MESSAGE)
    assert fetcher.requested == [url]


def test_gkmlpt_refused_api_is_not_requested_again_for_30_days(session):
    html, _, page_url, api_url, url = _gkmlpt_fixture()
    source = make_source(session, url)
    source.parser_config = {"list_api_open": "数字广东gkmlpt"}
    session.commit()
    refused = FetchResult(api_url, "http_error", final_url=api_url, http_status=403, text="")
    fetcher = RecordingFetcher({url: [page(page_url, html)], api_url: [refused]})
    check = check_source(session, source, fetcher, today=date(2026, 11, 12))
    assert check.status == "list_api_refused" and "403" in check.error
    session.refresh(source)
    assert source.parser_config["list_api_refused"]["url"] == api_url
    # 下一轮：栏目页还声明着同一个接口，30 天内不再请求
    again = RecordingFetcher({url: [page(page_url, html)]})
    check = check_source(session, source, again, today=date(2026, 11, 12))
    assert check.status == "list_api_refused" and again.requested == [url]
