"""从列表行文本和网址里解析发布日期。"""

import re
from datetime import date, timedelta

# 2026-09-24、2026/9/24、2026.09.24、2026年9月24日
_FULL = re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?")
# 只有月日：09-24、[09-24]；09/24、09.24 只认两位数，避免把"3.5 亿"当日期。
# 前后不能紧挨数字或分隔符，后面不能是"号""%"：避开电话号码和"〔2026〕1-2号"这类文号
_SHORT = re.compile(r"(?<![\d./\-])(?:(\d{1,2})-(\d{1,2})|(\d{2})[./](\d{2}))(?![\d:./\-号%])")
# 政府网站常见的正文网址：/202609/t20260924_4878106.html
_URL_DAY = re.compile(r"[/_]t?(20\d{2})(\d{2})(\d{2})[_./]")
# 允许略晚于今天（时区差、提前发布），更晚的视为误识别
_FUTURE_TOLERANCE = timedelta(days=2)


def _safe_date(year: int, month: int, day: int, today: date) -> date | None:
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    if d.year < 2000 or d > today + _FUTURE_TOLERANCE:
        return None
    return d


def _infer_year(month: int, day: int, today: date) -> date | None:
    # 年末前后：1 月 1 日的条目在 12 月 31 日出现，只可能是明年的（时区差）
    upcoming = _safe_date(today.year + 1, month, day, today)
    if upcoming:
        return upcoming
    return _safe_date(today.year, month, day, today) or _safe_date(
        today.year - 1, month, day, today
    )


def parse_date_detail(text: str, today: date | None = None) -> tuple[date | None, bool]:
    """返回（日期，是否写明了年份）。

    有完整日期时取第一个；只有"月-日"时取最靠后的一个（列表行的日期通常写在末尾），并推断年份。
    """
    today = today or date.today()
    for m in _FULL.finditer(text):
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), today)
        if d:
            return d, True
    for m in reversed(list(_SHORT.finditer(text))):
        month, day = (
            (int(m.group(1)), int(m.group(2)))
            if m.group(1)
            else (
                int(m.group(3)),
                int(m.group(4)),
            )
        )
        d = _infer_year(month, day, today)
        if d:
            return d, False
    return None, False


def parse_date(text: str, today: date | None = None) -> date | None:
    return parse_date_detail(text, today)[0]


def date_from_url(url: str, today: date | None = None) -> date | None:
    """很多政府网站的正文网址里带发布日期（如 t20260924_xxx.html），列表行没写日期时用它兜底。"""
    today = today or date.today()
    m = _URL_DAY.search(url)
    if not m:
        return None
    return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), today)
