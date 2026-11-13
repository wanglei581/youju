from datetime import date

import pytest

from youju.crawler.dates import date_from_url, parse_date, parse_date_detail
from youju.crawler.urls import normalize_url, page_dir, public_url, url_key


def test_http_443_is_https_only_for_that_port():
    old = "http://example.gov.cn:443/a.html?q=1#part"
    assert public_url(old) == "https://example.gov.cn/a.html?q=1#part"
    assert normalize_url("https://example.gov.cn/", old) == "https://example.gov.cn/a.html?q=1"
    assert public_url("http://example.gov.cn:80/a.html") == "http://example.gov.cn:80/a.html"
    assert public_url("https://example.gov.cn:443/a.html") == "https://example.gov.cn:443/a.html"


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
        # 月份区间不是日期，往后找真正的发布日期（襄阳财政："2026年1-6月"不是 2026-01-06）
        ("2026年1-6月襄阳市财政局规范性文件清理表 2026-06-30", date(2026, 6, 30)),
        ("2025年10-12月襄阳市财政局规范性文件清理表 2026-01-06", date(2026, 1, 6)),
        ("07-15 1-6月全市财政收支情况", date(2026, 7, 15)),
        ("2026 年 1-7 月某市经济运行情况 2026-08-20", date(2026, 8, 20)),
        # 日期后面紧跟时间、"至……月"、紧贴着"10月……"开头的标题，照常认
        ("2026-09-2410:30", date(2026, 9, 24)),
        ("2026年3月5日至6月", date(2026, 3, 5)),
        ("2026-09-0510月第三批项目名单", date(2026, 9, 5)),
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
        # 月份区间
        "2026年1-6月",
        "2026 年 1-7 月",
        "2025年10-12月财政收支情况",
        "1-6月全市财政收支情况",
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


def test_url_key_preserves_information_categories_only():
    from youju.crawler.urls import url_key

    base = "https://example.gov.cn/fgj/gkmlpt/index"
    assert url_key(base + "#xxgk-gkmlpt-8") != url_key(base + "#xxgk-gkmlpt-6")
    assert url_key(base) == "example.gov.cn/fgj/gkmlpt/index"
    assert url_key(base + "#abc") == url_key(base)
    # 只有数字广东分类的片段留着：页面内锚点、大汉信息公开分类树的片段照旧去掉
    assert url_key(base + "#xxgk-content") == url_key(base)
    assert url_key(base + "#xxgk-1229225203") == url_key(base)
    assert url_key(base + "#xxgk-gkmlpt-8") == "example.gov.cn/fgj/gkmlpt/index#xxgk-gkmlpt-8"


def test_host_and_origin_ignore_the_default_port():
    # 同一网站带不带默认端口是同一个网站：限速、robots、分组都按它（D-013，同站隔 3 秒）
    from youju.crawler.urls import host_of, origin_of

    assert host_of("https://www.Shanwei.gov.cn:443/a") == host_of("https://www.shanwei.gov.cn/b")
    assert host_of("http://x.gov.cn:80/a") == "x.gov.cn"
    assert host_of("https://x.gov.cn:8443/a") == "x.gov.cn:8443"
    assert host_of("http://x.gov.cn:443/a") == "x.gov.cn:443"  # http 的默认端口是 80
    assert origin_of("https://X.gov.cn:443/a") == "https://x.gov.cn"
    assert host_of("https://x.gov.cn:99999/a") == "x.gov.cn:99999"  # 端口写坏的照原样，不报错


def test_url_key_multiple_gkmlpt_categories():
    base = "https://example.gov.cn/gkmlpt/index"
    assert url_key(base + "#xxgk-gkmlpt-1,2") != url_key(base + "#xxgk-gkmlpt-1,3")
    assert url_key(base + "#xxgk-gkmlpt-p1,2").endswith("#xxgk-gkmlpt-p1,2")
    assert url_key(base + "#xxgk-gkmlpt-p1,p2").endswith("#xxgk-gkmlpt-p1,p2")
    assert url_key(base + "#xxgk-gkmlpt-p1,p2") != url_key(base + "#xxgk-gkmlpt-p1,2")
    assert url_key(base + "#ordinary") == url_key(base)
