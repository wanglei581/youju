"""从列表行文本和网址里解析发布日期。"""

import re
from datetime import date, timedelta

# 2026-09-24、2026/9/24、2026.09.24、2026年9月24日
_FULL = re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?")
# 只有月日的写法，如 [09-24]、09-24；前后不能紧挨数字或日期分隔符，避免把电话号码当日期
_SHORT = re.compile(r"(?<![\d./\-])(\d{1,2})-(\d{1,2})(?![\d:./\-])")
# 政府网站常见的正文网址：/202609/t20260924_4878106.html
_URL_DAY = re.compile(r"[/_]t?(20\d{2})(\d{2})(\d{2})[_./]")


def _safe_date(year: int, month: int, day: int, today: date) -> date | None:
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    # 允许略晚于今天（时区和提前发布），更晚的视为误识别
    if d.year < 2000 or d > today + timedelta(days=2):
        return None
    return d


def parse_date(text: str, today: date | None = None) -> date | None:
    """返回文本中的第一个合法日期；没有完整日期时尝试"月-日"并推断年份。"""
    today = today or date.today()
    for m in _FULL.finditer(text):
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), today)
        if d:
            return d
    for m in _SHORT.finditer(text):
        month, day = int(m.group(1)), int(m.group(2))
        d = _safe_date(today.year, month, day, today)
        if d is None:
            # 今年的这个日期还没到，说明是去年的
            d = _safe_date(today.year - 1, month, day, today)
        if d:
            return d
    return None


def date_from_url(url: str, today: date | None = None) -> date | None:
    """很多政府网站的正文网址里带发布日期（如 t20260924_xxx.html），列表行没写日期时用它兜底。"""
    today = today or date.today()
    m = _URL_DAY.search(url)
    if not m:
        return None
    return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), today)
