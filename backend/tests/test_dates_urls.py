from datetime import date

import pytest

from youju.crawler.dates import date_from_url, parse_date
from youju.crawler.urls import normalize_url

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
        # 今年的 12 月还没到，应推断为去年
        ("12-30", date(2025, 12, 30)),
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
    ],
)
def test_parse_date_rejects_non_dates(text: str) -> None:
    assert parse_date(text, TODAY) is None


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
