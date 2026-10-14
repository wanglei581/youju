"""原网页还在不在：连续两次（隔天）404/410 才算失效，能打开就恢复，403、超时不改状态。"""

from datetime import UTC, date, datetime, timedelta

from youju.crawler.fetcher import FetchResult
from youju.link_check import check_links
from youju.models import Document, Source

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)  # 北京时间 10:00


class FakeFetcher:
    def __init__(self, answers):
        self.answers = answers
        self.seen = []

    def fetch(self, url):
        self.seen.append(url)
        answer = self.answers.get(url, ("ok", 200))
        status, code = answer
        return FetchResult(url=url, status=status, http_status=code)


def make_source(session, rights_level=1):
    source = Source(
        level="省级",
        province="北京",
        dept="科技",
        dept_full_name="北京市科学技术委员会",
        column_name="通知公告",
        url="https://kw.beijing.gov.cn/zwgk/tzgg/",
        fetch_mode="static",
        enabled=True,
        parser_config={},
        rights_level=rights_level,
    )
    session.add(source)
    session.commit()
    return source


def make_doc(session, source, n, *, published=date(2026, 10, 10), taken_down=False):
    doc = Document(
        source_id=source.id,
        url=f"https://kw.beijing.gov.cn/{n}.html",
        url_key=f"kw.beijing.gov.cn/{n}.html",
        title=f"关于开展第{n}项申报工作的通知",
        publish_date=published,
        first_seen_at=NOW,
        last_seen_at=NOW,
        taken_down_at=NOW if taken_down else None,
    )
    session.add(doc)
    session.commit()
    return doc


def test_gone_only_after_two_404s_a_day_apart_and_recovers_on_200(session):
    source = make_source(session)
    doc = make_doc(session, source, 1)
    fake = FakeFetcher({doc.url: ("http_error", 404)})
    counts = check_links(session, fetcher=fake, now=NOW, max_workers=1)
    session.refresh(doc)
    assert counts["suspect"] == 1 and doc.link_suspect_at is not None and doc.link_gone_at is None
    # 不到 20 小时不再查
    assert check_links(session, fetcher=fake, now=NOW + timedelta(hours=10))["checked"] == 0
    # 隔天还是 404：确认失效
    counts = check_links(session, fetcher=fake, now=NOW + timedelta(hours=21), max_workers=1)
    session.refresh(doc)
    assert counts["gone"] == 1 and doc.link_gone_at is not None
    # 网页恢复：一周后再查能打开，状态清空
    fake.answers[doc.url] = ("ok", 200)
    counts = check_links(session, fetcher=fake, now=NOW + timedelta(days=8), max_workers=1)
    session.refresh(doc)
    assert counts["ok"] == 1 and doc.link_gone_at is None and doc.link_suspect_at is None


def test_blocked_or_timeout_does_not_change_state(session):
    source = make_source(session)
    blocked = make_doc(session, source, 1)
    slow = make_doc(session, source, 2)
    fake = FakeFetcher({blocked.url: ("http_error", 403), slow.url: ("network_error", None)})
    counts = check_links(session, fetcher=fake, now=NOW, max_workers=1)
    session.refresh(blocked)
    session.refresh(slow)
    assert counts["unknown"] == 2
    for doc in (blocked, slow):
        assert doc.link_checked_at is not None
        assert doc.link_suspect_at is None and doc.link_gone_at is None


def test_selection_order_window_and_limit(session):
    source = make_source(session)
    old = make_doc(session, source, 1, published=date(2026, 6, 1))  # 超过 90 天，不查
    hidden = make_doc(session, source, 2, taken_down=True)  # 已下架，不查
    recent_ok = make_doc(session, source, 3)
    recent_ok.link_checked_at = NOW - timedelta(days=2)  # 一周内查过，不查
    suspect = make_doc(session, source, 4)
    suspect.link_checked_at = NOW - timedelta(hours=21)
    suspect.link_suspect_at = NOW - timedelta(hours=21)  # 待确认，最先查
    never = make_doc(session, source, 5)
    stale = make_doc(session, source, 6)
    stale.link_checked_at = NOW - timedelta(days=7)
    session.commit()
    fake = FakeFetcher({})
    counts = check_links(session, fetcher=fake, now=NOW, limit=2, max_workers=1)
    assert counts["checked"] == 2
    assert fake.seen == [suspect.url, never.url]
    fake.seen.clear()
    check_links(session, fetcher=fake, now=NOW, limit=10, max_workers=1)
    assert fake.seen == [stale.url]
    assert (
        old.url not in fake.seen and hidden.url not in fake.seen and recent_ok.url not in fake.seen
    )


def test_other_sources_rights_level_not_checked(session):
    source = make_source(session, rights_level=2)
    make_doc(session, source, 1)
    assert check_links(session, fetcher=FakeFetcher({}), now=NOW)["checked"] == 0
