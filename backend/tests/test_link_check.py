"""原网页还在不在：近 30 天的每天查；404/410、跳到 404 页、200 的撤稿页都算像是撤下了，隔天两次才算失效；
能打开就恢复；403、429、5xx、超时、证书错误、空白页（防机器人脚本）判断不了，不改状态。"""

from datetime import UTC, date, datetime, timedelta

import pytest

from youju import cli
from youju.crawler.fetcher import FetchResult
from youju.link_check import (
    TEMPLATE_MAX,
    _classify,
    check_links,
    title_on_page,
    visible_text,
    withdrawn_page,
)
from youju.models import Document, Source

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)  # 北京时间 10:00
TITLE = "自治区科技厅关于组织申报 2027年度自治区技术交易补助的通知"
# 10/22 本机实测的模板（宁夏科技厅 74619 撤稿后：HTTP 404；其余是不存在的页面回 200 的网站）
NINGXIA = (
    "<html><head><title>宁夏回族自治区科学技术厅</title></head><body><div class='box'>"
    "<p>您访问的页面已撤稿或删除</p><p>如您是在地址栏输入网址的，请确认其拼写正确，"
    "并注意网址的大小写字母区分。</p><p>了解宁夏地区更多信息，请访问 宁夏回族自治区人民政府网</p>"
    "</div></body></html>"
)
TEMPLATES = [
    "您访问的页面已撤稿或删除",  # 宁夏
    "该文章已不存在",
    "很抱歉，您所访问的页面不存在！ 系统将在 5 秒内为您跳转",  # 内蒙古
    "抱歉，您访问的网页不存在或已删除！ 5 秒后将自动返回首页",  # 海南
    "温馨提示：您访问的页面不存在或已删除",  # 财政部
    "抱歉，您访问的地址有错或页面不存在。 如您是在地址栏输入网址的，请确认其拼写正确",  # 生态环境部
    "抱歉，您访问的页面不存在或已删除 返回首页 10 秒后自动返回首页",  # 安徽工信厅
    "信息不存在。。",  # 云南人社厅
]


def page(body: str, title: str = "") -> str:
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


class FakeFetcher:
    """answers：网址 → (status, http_status) 或 FetchResult（要带页面内容时）。"""

    def __init__(self, answers):
        self.answers = answers
        self.seen = []

    def fetch(self, url):
        self.seen.append(url)
        answer = self.answers.get(url, ("ok", 200))
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, FetchResult):
            return answer
        status, code = answer
        if status == "ok":  # 能打开：一个正常的页面
            return html_result(url, page("<h1>关于开展申报工作的通知</h1><p>各有关单位：</p>"))
        return FetchResult(url=url, status=status, http_status=code, final_url=url)


def html_result(url, text, *, final_url=None, content_type="text/html; charset=utf-8"):
    return FetchResult(
        url=url,
        status="ok",
        http_status=200,
        final_url=final_url or url,
        text=text,
        content_type=content_type,
        content=text.encode(),
    )


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


def make_doc(session, source, n, *, published=date(2026, 10, 10), taken_down=False, host=None):
    host = host or "kw.beijing.gov.cn"
    doc = Document(
        source_id=source.id,
        url=f"https://{host}/{n}.html",
        url_key=f"{host}/{n}.html",
        title=f"关于开展第{n}项申报工作的通知",
        publish_date=published,
        first_seen_at=NOW,
        last_seen_at=NOW,
        taken_down_at=NOW if taken_down else None,
    )
    session.add(doc)
    session.commit()
    return doc


# ---------- 一次检查的结论 ----------


@pytest.mark.parametrize("words", TEMPLATES)
def test_withdrawn_templates_seen_on_real_sites(words):
    url = "https://kjt.example.gov.cn/t1.html"
    assert withdrawn_page(html_result(url, page(f"<p>{words}</p>", "某某厅")), url, TITLE)
    assert _classify(html_result(url, page(f"<p>{words}</p>")), url, TITLE) == "withdrawn_page"


def test_ningxia_withdrawn_page():
    url = "https://kjt.nx.gov.cn/kjdt/tzgg/202610/t20261021_5377444.html"
    # 74619 实际回的是 404（照旧算）；同样的页面回 200 也认得出
    assert (
        _classify(FetchResult(url=url, status="http_error", http_status=404), url, TITLE) == "404"
    )
    assert _classify(html_result(url, NINGXIA), url, TITLE) == "withdrawn_page"


@pytest.mark.parametrize(
    "final",
    [
        "https://kjt.nmg.gov.cn/404/40x.html",
        "https://dost.hainan.gov.cn/404.html",
        "http://www.mof.gov.cn/404.htm",
        "https://www.mee.gov.cn/404/index.shtml",
        "https://site.example.gov.cn/40x.html",
    ],
)
def test_redirect_to_error_page(final):
    url = "https://site.example.gov.cn/art/1.html"
    # 404 页上什么字都没有（只有图片）也算
    assert _classify(
        html_result(url, page("<img src='404.png'>"), final_url=final), url, TITLE
    ) == ("withdrawn_page")
    # 没跳转、网址本身不像 404 页：看页面内容
    assert _classify(html_result(url, page("<p>正文</p>")), url, TITLE) == "ok"


def test_redirect_elsewhere_is_not_an_error_page():
    url = "http://gxt.example.gov.cn:443/2092553596592201729.html"
    for final in (
        "https://gxt.example.gov.cn/2092553596592201729.html",
        "https://x.gov.cn/404x/a.html",
    ):
        assert (
            _classify(html_result(url, page(f"<h1>{TITLE}</h1>"), final_url=final), url, TITLE)
            == "ok"
        )


def test_real_notice_that_mentions_the_words_is_ok():
    url = "https://kjt.example.gov.cn/t1.html"
    # 页面里有这份通知的标题：照常是能打开（正文里碰巧写了"信息不存在"）
    body = f"<h1>{TITLE}</h1><p>申报单位填报的信息不存在虚假的，方可推荐。</p>"
    assert _classify(html_result(url, page(body)), url, TITLE) == "ok"
    # 标题只在 <meta> 里（天津科技局正文靠脚本加载）也算有
    meta = f"<html><head><meta name='ArticleTitle' content='{TITLE}'></head><body><p>页面不存在</p></body></html>"
    assert _classify(html_result(url, meta), url, TITLE) == "ok"
    # 很长的页面（超过模板的长度）里碰巧有这些话：当成正常页面
    long = page("<p>页面不存在</p>" + "<p>申报说明。</p>" * (TEMPLATE_MAX // 5))
    assert len(visible_text(long)) > TEMPLATE_MAX
    assert _classify(html_result(url, long), url, TITLE) == "ok"
    # 只有"不存在""删除""撤稿"不算（通知里常有"不存在弄虚作假""予以删除""论文撤稿"），
    # 我们存的标题和页面上的对不上也一样
    for words in (
        "经核查不存在弄虚作假情形，相关内容予以删除。",
        "对存在学术不端的论文，按程序办理撤稿，撤稿后三年内不得申报。",
        "申报内容不存在知识产权纠纷，相关内容已删除个人信息。",
    ):
        plain = page(f"<h1>关于做好科研诚信工作的说明</h1><p>{words}</p>")
        assert _classify(html_result(url, plain), url, TITLE) == "ok", words


def test_blank_or_unknown_cannot_tell():
    url = "https://www.mohrss.gov.cn/t1.html"
    # 人社部的防机器人脚本：没有可见文字，正常页面也这样
    challenge = "<script>document.cookie='__tst_status=1';location.href=location.href;</script>"
    assert _classify(html_result(url, challenge), url, TITLE) == "unknown"
    assert _classify(html_result(url, ""), url, TITLE) == "unknown"
    # 回了 200 却什么都没有（没有内容类型、没有内容）
    empty = FetchResult(url=url, status="ok", http_status=200, final_url=url)
    assert _classify(empty, url, TITLE) == "unknown"
    for status, code in (
        ("http_error", 403),
        ("http_error", 412),
        ("http_error", 429),
        ("http_error", 500),
        ("http_error", 503),
        ("network_error", None),
        ("ssl_error", None),
        ("robots_error", None),
        ("robots_disallowed", None),
        ("unsupported_host", None),
        ("too_large", 200),
    ):
        assert _classify(FetchResult(url=url, status=status, http_status=code), url, TITLE) == (
            "unknown"
        ), (status, code)
    assert (
        _classify(FetchResult(url=url, status="http_error", http_status=410), url, TITLE) == "410"
    )


def test_files_and_xhtml_pages_are_ok():
    url = "https://kjt.example.gov.cn/1.pdf"
    pdf = FetchResult(url=url, status="ok", http_status=200, final_url=url, content=b"%PDF-1.7 ...")
    assert _classify(pdf, url, TITLE) == "ok"
    doc = FetchResult(
        url=url,
        status="ok",
        http_status=200,
        final_url=url,
        text="\x00\x01",
        content=b"\xd0\xcf\x11\xe0",
        content_type="application/msword",
    )
    assert _classify(doc, url, TITLE) == "ok"
    xhtml = '<?xml version="1.0" encoding="utf-8"?>' + page(f"<h1>{TITLE}</h1>")
    assert visible_text(xhtml).startswith(TITLE)
    assert _classify(html_result(url, xhtml), url, TITLE) == "ok"


def test_title_on_page_tolerates_stored_title_differences():
    # 我们存的标题前面带"》""·"、中间有空格，页面上是 HTML 实体：照样认得
    assert title_on_page(
        "》 2026年自治区技术转移机构拟认定名单公示",
        page("<h1>2026年自治区技术转移机构拟认定名单公示</h1>"),
    )
    assert title_on_page(
        "关于“创客中国” 大赛的公示", page("<h1>关于&ldquo;创客中国&rdquo;大赛的公示</h1>")
    )
    # 财政部存的是新闻标题，页面上是正式标题：大半片段对得上
    assert title_on_page(
        "《紧急采购管理暂行办法》印发", page("<h1>关于印发《紧急采购管理暂行办法》的通知</h1>")
    )
    # 标题被 <br>、<span> 切开（源码里夹着标签名）也认得
    assert title_on_page(TITLE, page(f"<h1>{TITLE[:10]}<br/><span>{TITLE[10:]}</span></h1>"))
    # 正文靠脚本加载、标题只在脚本里的
    assert title_on_page(TITLE, f"<html><body><script>var t = '{TITLE}';</script></body></html>")
    # 撤稿页里只有站名，和标题开头的机关名重了几个字也不算
    assert not title_on_page(TITLE, NINGXIA)
    assert not title_on_page("", NINGXIA)
    assert title_on_page("短标题", page("<p>短标题</p>")) and not title_on_page("短标题", NINGXIA)


# ---------- 记账：隔天两次、判断不了的不算数 ----------


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
    # 网页恢复：近 30 天的每天查，第二天能打开就清空
    fake.answers[doc.url] = ("ok", 200)
    counts = check_links(session, fetcher=fake, now=NOW + timedelta(hours=42), max_workers=1)
    session.refresh(doc)
    assert counts["ok"] == 1 and doc.link_gone_at is None and doc.link_suspect_at is None


def test_withdrawn_page_twice_a_day_apart_marks_gone(session):
    source = make_source(session)
    doc = make_doc(session, source, 1)
    fake = FakeFetcher({doc.url: html_result(doc.url, NINGXIA)})
    counts = check_links(session, fetcher=fake, now=NOW, max_workers=1)
    assert counts["suspect"] == 1 and counts["withdrawn_page"] == 1
    counts = check_links(session, fetcher=fake, now=NOW + timedelta(hours=21), max_workers=1)
    session.refresh(doc)
    assert counts["gone"] == 1 and counts["withdrawn_page"] == 1 and doc.link_gone_at is not None


def test_cannot_tell_neither_counts_nor_clears(session):
    """第一次像是撤下了 → 第二天超时 → 第三天还是 404：中间判断不了的不清零，第三天确认。"""
    source = make_source(session)
    doc = make_doc(session, source, 1)
    fake = FakeFetcher({doc.url: ("http_error", 404)})
    check_links(session, fetcher=fake, now=NOW, max_workers=1)
    for status, code in (("network_error", None), ("http_error", 429), ("ssl_error", None)):
        fake.answers[doc.url] = (status, code)
        counts = check_links(session, fetcher=fake, now=NOW + timedelta(hours=21), max_workers=1)
        session.refresh(doc)
        assert counts["unknown"] == 1 and doc.link_suspect_at is not None
        assert doc.link_gone_at is None
        doc.link_checked_at = NOW  # 回到第一天之后，下一种再试
        session.commit()
    fake.answers[doc.url] = html_result(doc.url, "<script>location.href='/'</script>")
    counts = check_links(session, fetcher=fake, now=NOW + timedelta(hours=21), max_workers=1)
    assert counts["unknown"] == 1
    fake.answers[doc.url] = ("http_error", 410)
    counts = check_links(session, fetcher=fake, now=NOW + timedelta(hours=44), max_workers=1)
    session.refresh(doc)
    assert counts["gone"] == 1 and doc.link_gone_at is not None


def test_one_good_check_in_between_starts_over(session):
    source = make_source(session)
    doc = make_doc(session, source, 1)
    fake = FakeFetcher({doc.url: ("http_error", 404)})
    check_links(session, fetcher=fake, now=NOW, max_workers=1)
    fake.answers[doc.url] = ("ok", 200)
    check_links(session, fetcher=fake, now=NOW + timedelta(hours=21), max_workers=1)
    fake.answers[doc.url] = ("http_error", 404)
    counts = check_links(session, fetcher=fake, now=NOW + timedelta(hours=42), max_workers=1)
    session.refresh(doc)
    assert counts["suspect"] == 1 and doc.link_gone_at is None


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


# ---------- 查哪些、多勤 ----------


def test_selection_order_window_and_limit(session):
    source = make_source(session)
    old = make_doc(session, source, 1, published=date(2026, 6, 1))  # 超过 90 天，不查
    hidden = make_doc(session, source, 2, taken_down=True)  # 已下架，不查
    today_ok = make_doc(session, source, 3)
    today_ok.link_checked_at = NOW - timedelta(hours=5)  # 近 30 天的今天查过了，不查
    suspect = make_doc(session, source, 4)
    suspect.link_checked_at = NOW - timedelta(hours=21)
    suspect.link_suspect_at = NOW - timedelta(hours=21)  # 待确认，最先查
    never = make_doc(session, source, 5)
    stale = make_doc(session, source, 6)
    stale.link_checked_at = NOW - timedelta(days=7)
    yesterday = make_doc(session, source, 7)
    yesterday.link_checked_at = NOW - timedelta(hours=21)  # 近 30 天的：隔天就查
    older_fresh = make_doc(session, source, 8, published=date(2026, 9, 1))
    older_fresh.link_checked_at = NOW - timedelta(days=2)  # 31–90 天的：一周内查过，不查
    older_due = make_doc(session, source, 9, published=date(2026, 8, 20))
    older_due.link_checked_at = NOW - timedelta(days=7)
    older_never = make_doc(session, source, 10, published=date(2026, 9, 2))
    session.commit()
    fake = FakeFetcher({})
    counts = check_links(session, fetcher=fake, now=NOW, limit=2, max_workers=1)
    assert counts["checked"] == 2
    # 该查的 6 份（待确认 1、近 30 天 3、31–90 天 2），这次查了 2 份
    assert counts["left"] == 4
    assert fake.seen == [suspect.url, never.url]
    fake.seen.clear()
    counts = check_links(session, fetcher=fake, now=NOW, limit=10, max_workers=1)
    # 近 30 天的先查（上次检查早的先），再是 31–90 天的（没查过的先）
    assert fake.seen == [stale.url, yesterday.url, older_never.url, older_due.url]
    assert counts["left"] == 0
    for skipped in (old, hidden, today_ok, older_fresh):
        assert skipped.url not in fake.seen


def test_recent_window_is_by_beijing_publish_date(session):
    """近 30 天按北京时间的发布日期算：第 30 天的每天查，第 31 天的照旧一周一次。"""
    source = make_source(session)
    edge = make_doc(session, source, 1, published=date(2026, 9, 15))  # 北京时间 10/15 的 30 天前
    beyond = make_doc(session, source, 2, published=date(2026, 9, 14))
    for doc in (edge, beyond):
        doc.link_checked_at = NOW - timedelta(days=2)
    session.commit()
    fake = FakeFetcher({})
    check_links(session, fetcher=fake, now=NOW, max_workers=1)
    assert fake.seen == [edge.url]


def test_other_sources_rights_level_not_checked(session):
    source = make_source(session, rights_level=2)
    make_doc(session, source, 1)
    assert check_links(session, fetcher=FakeFetcher({}), now=NOW)["checked"] == 0


def test_one_unexpected_error_does_not_stop_the_batch(session, caplog):
    """一份出了意外（不是网络错误，是程序没想到的）：算判断不了、记日志，别的照常查完写进去。"""
    source = make_source(session)
    first = make_doc(session, source, 1, host="a.example.gov.cn")
    second = make_doc(session, source, 2, host="b.example.gov.cn")
    third = make_doc(session, source, 3, host="b.example.gov.cn")
    fake = FakeFetcher({second.url: RuntimeError("意外"), third.url: ("http_error", 404)})
    counts = check_links(session, fetcher=fake, now=NOW, max_workers=2)
    assert counts["checked"] == 3 and counts["unknown"] == 1 and counts["suspect"] == 1
    assert counts["left"] == 0
    assert "原网页检查出错" in caplog.text and "RuntimeError" in caplog.text
    session.expire_all()
    for doc in (first, second, third):
        assert session.get(Document, doc.id).link_checked_at is not None
    assert session.get(Document, second.id).link_suspect_at is None


def test_each_site_is_written_as_soon_as_it_is_done(session):
    """查完一个网站就写进数据库：后面被打断（部署重启、超时），前面查过的不丢。"""
    source = make_source(session)
    first = make_doc(session, source, 1, host="a.example.gov.cn")
    second = make_doc(session, source, 2, host="b.example.gov.cn")
    fake = FakeFetcher({second.url: KeyboardInterrupt()})
    with pytest.raises(KeyboardInterrupt):
        check_links(session, fetcher=fake, now=NOW, max_workers=1)
    session.rollback()
    session.expire_all()
    assert session.get(Document, first.id).link_checked_at is not None
    assert session.get(Document, second.id).link_checked_at is None


def test_suspect_and_gone_older_than_90_days_are_still_checked(session):
    """第 90 天第一次像是撤下了的，过了 90 天也要等到确认；已确认失效的过了 90 天照常一周查一次，
    网页恢复了能清掉（不然一直不能 AI 解读）。没出过问题的过了 90 天就不查了。"""
    source = make_source(session)
    suspect = make_doc(session, source, 1, published=date(2026, 7, 1))
    suspect.link_checked_at = suspect.link_suspect_at = NOW - timedelta(hours=21)
    gone = make_doc(session, source, 2, published=date(2026, 5, 1))
    gone.link_suspect_at = NOW - timedelta(days=30)
    gone.link_gone_at = NOW - timedelta(days=29)
    gone.link_checked_at = NOW - timedelta(days=7)
    gone_fresh = make_doc(session, source, 3, published=date(2026, 5, 2))
    gone_fresh.link_suspect_at = gone_fresh.link_gone_at = NOW - timedelta(days=30)
    gone_fresh.link_checked_at = NOW - timedelta(days=2)  # 一周内查过
    fine = make_doc(session, source, 4, published=date(2026, 7, 1))
    fine.link_checked_at = NOW - timedelta(days=7)
    session.commit()
    fake = FakeFetcher({suspect.url: ("http_error", 404)})
    counts = check_links(session, fetcher=fake, now=NOW, max_workers=1)
    assert fake.seen == [suspect.url, gone.url]
    session.refresh(suspect)
    session.refresh(gone)
    assert counts["gone"] == 1 and suspect.link_gone_at is not None
    assert counts["ok"] == 1 and gone.link_gone_at is None and gone.link_suspect_at is None


def test_counts_suspects_waiting_more_than_three_days(session):
    """第一次像是撤下了以后一直判断不了（网站一直超时）：3 天还没确认的单独数出来。"""
    source = make_source(session)
    waiting = make_doc(session, source, 1)
    waiting.link_suspect_at = NOW - timedelta(days=4)
    waiting.link_checked_at = NOW - timedelta(hours=21)
    recent = make_doc(session, source, 2)
    recent.link_suspect_at = recent.link_checked_at = NOW - timedelta(hours=21)
    session.commit()
    # 不再查的（来源后来改成不是一级公开的）不算
    level2 = Source(
        level="省级",
        province="北京",
        dept="工信",
        dept_full_name="北京市经济和信息化局",
        column_name="转载",
        url="https://jxj.beijing.gov.cn/zz/",
        fetch_mode="static",
        enabled=True,
        parser_config={},
        rights_level=2,
    )
    session.add(level2)
    session.commit()
    other = make_doc(session, level2, 3, host="jxj.beijing.gov.cn")
    other.link_suspect_at = NOW - timedelta(days=4)
    session.commit()
    fake = FakeFetcher({waiting.url: ("network_error", None), recent.url: ("network_error", None)})
    counts = check_links(session, fetcher=fake, now=NOW, max_workers=1)
    assert counts["unknown"] == 2 and counts["stale_suspect"] == 1
    assert check_links(session, fetcher=fake, now=NOW, max_workers=1)["stale_suspect"] == 1


def test_cli_prints_counts(session, session_factory, monkeypatch, capsys):
    source = make_source(session)
    doc = make_doc(session, source, 1)
    fake = FakeFetcher({doc.url: html_result(doc.url, NINGXIA)})
    import youju.link_check as link_check

    real = link_check.check_links
    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    monkeypatch.setattr(
        link_check,
        "check_links",
        lambda s, limit, until: real(s, limit=limit, until=until, fetcher=fake, now=NOW),
    )
    assert cli.main(["check-links", "--limit", "5"]) == 0
    out = capsys.readouterr().out
    assert "检查原网页 1 份" in out and "第一次像是撤下了待确认 1" in out
    assert "网站回 200 的撤稿页、404 页 1 次" in out and "该查还没轮到 0 份" in out
    assert "3 天还没确认的 0 份" in out


def test_stops_when_time_is_up_and_counts_the_rest_as_left(session):
    """开始后 budget 秒就不再开始新的检查（5:10 开始，6:10 起不开新的，6:30 抓取前停下）：
    没查完的算还没轮到，第二天接着查。"""
    source = make_source(session)
    docs = [make_doc(session, source, n) for n in range(1, 4)]
    ticks = iter(range(0, 1000, 6))  # 每问一次时间过 6 秒
    fake = FakeFetcher({})
    counts = check_links(
        session, fetcher=fake, now=NOW, max_workers=1, budget=10, clock=lambda: next(ticks)
    )
    # 开始时 0 秒，到时间 10 秒：第一份（6 秒）查了，第二份（12 秒）不查
    assert counts["checked"] == 1 and counts["left"] == 2
    assert fake.seen == [docs[0].url]
    counts = check_links(session, fetcher=fake, now=NOW + timedelta(minutes=5), max_workers=1)
    assert counts["checked"] == 2 and counts["left"] == 0


def test_until_in_the_past_checks_nothing(session):
    """定时任务带 --until 06:10：晚开始、已经过了 6:10，就一份也不查，都算还没轮到。"""
    source = make_source(session)
    for n in range(1, 4):
        make_doc(session, source, n)
    fake = FakeFetcher({})
    counts = check_links(session, fetcher=fake, now=NOW, until=NOW - timedelta(minutes=1))
    assert counts["checked"] == 0 and counts["left"] == 3 and fake.seen == []
    # 还没到：照常查
    counts = check_links(session, fetcher=fake, now=NOW, until=NOW + timedelta(minutes=50))
    assert counts["checked"] == 3 and counts["left"] == 0


def test_fetch_stopped_by_deadline_is_not_counted_as_checked(session):
    """抓取器到了期限放下的（deadline）：不是网站的问题，不记检查时间，算还没轮到。"""
    source = make_source(session)
    first = make_doc(session, source, 1)
    second = make_doc(session, source, 2)
    fake = FakeFetcher({first.url: FetchResult(url=first.url, status="deadline")})
    counts = check_links(session, fetcher=fake, now=NOW, max_workers=1)
    assert counts["checked"] == 0 and counts["left"] == 2
    assert fake.seen == [first.url]  # 同一网站剩下的也不再查
    session.expire_all()
    assert session.get(Document, first.id).link_checked_at is None
    assert session.get(Document, second.id).link_checked_at is None


def test_cli_until_option(session, session_factory, monkeypatch, capsys):
    import youju.link_check as link_check

    seen = {}

    def fake_check(s, limit, until):
        seen.update(limit=limit, until=until)
        return {
            k: 0 for k in ("checked", "ok", "suspect", "gone", "unknown", "withdrawn_page", "left")
        } | {"stale_suspect": 0}

    monkeypatch.setattr(cli, "session_factory", lambda: session_factory)
    monkeypatch.setattr(link_check, "check_links", fake_check)
    assert cli.main(["check-links", "--limit", "3000", "--until", "6:10"]) == 0
    until = seen["until"]
    assert seen["limit"] == 3000 and (until.hour, until.minute, until.second) == (6, 10, 0)
    assert until.utcoffset() == timedelta(hours=8)
    assert cli.main(["check-links"]) == 0 and seen["until"] is None
    for bad in ("6", "25:00", "06:61", "六点"):
        with pytest.raises(SystemExit):
            cli.main(["check-links", "--until", bad])


def test_redirect_to_empty_404_page_is_withdrawn():
    """跳到 404 页面、那页是空的 200：照样算撤下了（不当成判断不了）。"""
    url = "https://site.example.gov.cn/art/1.html"
    empty = FetchResult(
        url=url, status="ok", http_status=200, final_url="https://site.example.gov.cn/404.html"
    )
    assert _classify(empty, url, TITLE) == "withdrawn_page"
