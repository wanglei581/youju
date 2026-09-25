from datetime import date

import pytest

from youju.crawler.dates import date_from_url, parse_date, parse_date_detail
from youju.crawler.urls import normalize_url, page_dir, url_key

TODAY = date(2026, 9, 26)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-09-24", date(2026, 9, 24)),
        ("(2025-11-25)", date(2025, 11, 25)),
        ("2026/9/4", date(2026, 9, 4)),
        ("2026.09.24", date(2026, 9, 24)),
        ("发布时间：2026年9月24日", date(2026, 9, 24)),
        ("[09-24]", date(2026, 9, 24)),
        ("09/20", date(2026, 9, 20)),
        ("09.24", date(2026, 9, 24)),
        # 今年的 12 月还没到，应推断为去年
        ("12-30", date(2025, 12, 30)),
        # 文号里的"1-2号"不是日期；同一行有多个月-日时取最靠后的
        ("成科规〔2026〕1-2号 09-20", date(2026, 9, 20)),
        ("第1-2期 09-20", date(2026, 9, 20)),
        # 日期后面跟着期号、金额，不能被它们盖过
        ("09-20 第1-2期", date(2026, 9, 20)),
        ("09-20 补助10.25万元", date(2026, 9, 20)),
        ("成科规〔2026〕1-2 号 09-20", date(2026, 9, 20)),
    ],
)
def test_parse_date_formats(text: str, expected: date) -> None:
    assert parse_date(text, TODAY) == expected


@pytest.mark.parametrize(
    "text",
    [
        "没有日期",
        "2026-13-01",  # 月份不合法
        "2026-02-30",  # 日期不存在
        "2027-01-01",  # 晚于今天太多，视为误识别
        "电话：0532-85911234",  # 电话号码不是日期
        "10:30",
        "成科规〔2026〕1-2号",
        "成科规〔2026〕1-2 号",
        "第1-2期",
        "10.25万元",
        "增长 3.5 亿元",
        "占比 12.50%",
    ],
)
def test_parse_date_rejects_non_dates(text: str) -> None:
    assert parse_date(text, TODAY) is None


@pytest.mark.parametrize(
    ("today", "text", "expected"),
    [
        # 只写月-日时取不晚于今天的最近一个：网站和程序都用北京时间，列表上没有"明天"的条目。
        # 12 月 31 日看到的"01-01"是当年 1 月 1 日，不会排到 12 月的条目前面
        (date(2026, 12, 31), "01-01", date(2026, 1, 1)),
        (date(2026, 1, 1), "12-31", date(2025, 12, 31)),
        (date(2026, 9, 26), "01-15", date(2026, 1, 15)),
        (date(2026, 9, 26), "09-26", date(2026, 9, 26)),
        (date(2026, 9, 26), "09-28", date(2025, 9, 28)),
    ],
)
def test_short_date_year_inference(today: date, text: str, expected: date) -> None:
    assert parse_date(text, today) == expected


def test_parse_date_detail_reports_whether_year_was_written() -> None:
    assert parse_date_detail("2026-09-24", TODAY) == (date(2026, 9, 24), True)
    assert parse_date_detail("09-24", TODAY) == (date(2026, 9, 24), False)
    assert parse_date_detail("没有日期", TODAY) == (None, False)


def test_date_from_url() -> None:
    assert date_from_url("https://a.gov.cn/zwgk/tzgg/202609/t20260923_4878106.html", TODAY) == date(
        2026, 9, 23
    )
    assert date_from_url("https://a.gov.cn/art/2026/9/23/art_1.html", TODAY) is None


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("./202609/t1.html", "https://kw.beijing.gov.cn/zwgk/tzgg/202609/t1.html"),
        ("../zcwj/", "https://kw.beijing.gov.cn/zwgk/zcwj/"),
        ("/art/2026/9/23/a.html#top", "https://kw.beijing.gov.cn/art/2026/9/23/a.html"),
        ("  /a.html\r\n", "https://kw.beijing.gov.cn/a.html"),
        ("HTTP://Example.GOV.cn/x?id=1", "http://example.gov.cn/x?id=1"),
        ("javascript:void(0)", None),
        ("mailto:a@b.cn", None),
        ("#", None),
        ("", None),
    ],
)
def test_normalize_url(href: str, expected: str | None) -> None:
    assert normalize_url("https://kw.beijing.gov.cn/zwgk/tzgg/", href) == expected


@pytest.mark.parametrize(
    "variant",
    [
        "http://kjt.example.gov.cn/art/1.html",
        "https://kjt.example.gov.cn/art/1.html",
        "https://KJT.example.gov.cn:443/art/1.html",
        "http://kjt.example.gov.cn:80/art/1.html",
        "https://kjt.example.gov.cn/art/1.html;jsessionid=AAA",
        "https://kjt.example.gov.cn/art/./x/../1.html",
        "https://kjt.example.gov.cn/art/1.html?jsessionid=BBB",
    ],
)
def test_url_key_treats_variants_as_same_document(variant: str) -> None:
    assert url_key(variant) == "kjt.example.gov.cn/art/1.html"


def test_url_key_sorts_query_and_drops_session_params() -> None:
    a = url_key("https://a.gov.cn/c.jsp?id=9&type=1")
    assert a == url_key("https://a.gov.cn/c.jsp?type=1&id=9")
    assert a == url_key("https://a.gov.cn/c.jsp?PHPSESSID=x&type=1&id=9")
    assert a == "a.gov.cn/c.jsp?id=9&type=1"


def test_url_key_keeps_what_distinguishes_documents() -> None:
    assert url_key("https://a.gov.cn/art/1.html") != url_key("https://a.gov.cn/art/2.html")
    assert url_key("https://a.gov.cn/c.jsp?id=9") != url_key("https://a.gov.cn/c.jsp?id=10")
    assert url_key("https://a.gov.cn:8443/art/1.html") == "a.gov.cn:8443/art/1.html"
    assert url_key("https://a.gov.cn/zwgk/tzgg/") == "a.gov.cn/zwgk/tzgg/"
    assert url_key("https://a.gov.cn") == "a.gov.cn/"
    # sid 常用来区分栏目或站点，不当作会话参数
    assert url_key("https://a.gov.cn/show.asp?sid=1&id=100") != url_key(
        "https://a.gov.cn/show.asp?sid=2&id=100"
    )


def test_url_key_folds_directory_index_page() -> None:
    assert url_key("https://a.gov.cn/tzgg/index.html") == url_key("https://a.gov.cn/tzgg/")
    assert url_key("https://a.gov.cn/tzgg/index.shtml") == "a.gov.cn/tzgg/"
    assert url_key("https://a.gov.cn/tzgg/list.html") == "a.gov.cn/tzgg/list.html"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://a.gov.cn/zwgk/tzgg/", "/zwgk/tzgg/"),
        ("https://a.gov.cn/zwgk/tzgg/index.html", "/zwgk/tzgg/"),
        ("https://a.gov.cn/col/col1/index.html?uid=2", "/col/col1/"),
        ("https://a.gov.cn", "/"),
        # 末段没有扩展名：当作目录
        ("https://a.gov.cn/zwgk/tzgg", "/zwgk/tzgg/"),
    ],
)
def test_page_dir(url: str, expected: str) -> None:
    assert page_dir(url) == expected
