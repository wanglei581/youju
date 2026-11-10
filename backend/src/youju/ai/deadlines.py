"""用 AI 抽截止日期候选，输出和规则抽取同一种结构，方便互相印证；结果仍要人工核对。

防编造：证据必须是原文里的原句，而且句子里要真的写着这个日期；日期要在发布日附近。
"""

import re
from datetime import date, time, timedelta

from youju.extract.deadlines import _DATE, DeadlineCandidate, _evidence

from .client import ChatClient

KINDS = ("企业申报截止", "推荐或审核截止", "材料报送截止", "其他")
CONFIDENCES = ("高", "中", "低")

# 截止检查的版本号，存进每条 AI 检查记录（AICheck.result 的 version）。改提示词或解析规则就加 1；
# 旧版本的结果不会自动重跑，要重跑用 youju ai-recheck-deadlines（会覆盖旧结果，要先征得同意）。
# 1：最初版本，记录里直接存列表；
# 2：原句按全文核对（可以跨"；"和换行），"24:00"记成当天、不写时刻，
#    "9:00"这类一位数的钟点也认（D-099 期 0）
RESULT_VERSION = 2

_SYSTEM = """你是截止日期抽取助手。必须只返回 JSON 对象，字段 deadlines 为数组。
每项字段：date（YYYY-MM-DD）、time（HH:MM 或空）、label（企业申报截止/推荐或审核截止/
材料报送截止/其他）、evidence（原文连续原句，要包含这个日期）、confidence（高/中/低）。
只提取正文明确写出的截止、截至、报送、申报等日期，不要编造；日期不确定就不输出。"""


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _prepare_text(text: str) -> str:
    """正文过长时，优先保留写着日期或截止、报送、申报的段落，总长不超过 6000 字。"""
    if len(text) <= 6000:
        return text
    paragraphs = re.split(r"\n\s*\n|\n", text)
    cues = re.compile(r"\d{1,4}\s*[年./-]\s*\d{1,2}|截止|截至|前|报送|申报")
    picked = [part for part in paragraphs if cues.search(part)]
    rest = [part for part in paragraphs if part not in picked]
    return "\n".join(picked + rest)[:6000]


def _date_span(evidence: str, due: date) -> tuple[int, int] | None:
    """证据句里写着这个日期的位置；写了年份的，年份也要对得上。"""
    for match in _DATE.finditer(evidence):
        year = match.group("year")
        if (
            int(match.group("month")) == due.month
            and int(match.group("day")) == due.day
            and (year is None or int(year) == due.year)
        ):
            return match.start(), match.end()
    return None


def stored_items(result: object) -> list:
    """AI 检查记录（AICheck.result）里的截止项：第 1 版直接存列表，第 2 版起存
    {"version": 版本号, "items": [...]}；格式不对的当作没有。"""
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and isinstance(result.get("items"), list):
        return result["items"]
    return []


def stored_version(result: object) -> int:
    """AI 检查记录是哪一版截止检查做的；第 1 版存的是列表，认不出的算 0（按旧版本对待）。"""
    if isinstance(result, list):
        return 1
    if isinstance(result, dict) and isinstance(result.get("version"), int):
        return result["version"]
    return 0


def _clock(value: str) -> time | None | bool:
    """AI 给的时刻：认"HH:MM"和"H:MM"；"24:00"是当天结束，和规则一样只记日期（返回 None）；
    其他写法返回 False（这一项不可信，丢掉）。"""
    if not value:
        return None
    if value in ("24:00", "24:00:00"):
        return None
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", value)
    if not match:
        return False
    return time(int(match.group(1)), int(match.group(2)))


def extract_deadlines_with_ai(
    client: ChatClient, title: str, text: str, publish_date: date | None
) -> list[DeadlineCandidate]:
    """AI 给出的截止候选，按规则抽取的格式返回；不可信的逐条丢掉。"""
    result = client.complete_json(_SYSTEM, f"标题：{title}\n正文：{_prepare_text(text)}")
    items = result.get("deadlines", [])
    if not isinstance(items, list):
        return []
    # 原句按全文逐字核对（去掉空白）：可以跨"；"和换行。以前按单句核对，跨了的整条丢掉（D-099 期 0）
    full = _compact(text)
    lower = publish_date - timedelta(days=30) if publish_date else None
    upper = publish_date + timedelta(days=730) if publish_date else None
    output: list[DeadlineCandidate] = []
    seen: set[tuple[date, time | None]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            due = date.fromisoformat(str(item["date"]))
            # 跨行的原句存成一行
            evidence = " ".join(str(item["evidence"]).split())
            clock = str(item.get("time") or "").strip()
        except (KeyError, TypeError, ValueError):
            continue
        compact = _compact(evidence)
        if not compact or compact not in full:
            continue  # 不是原文里的原句
        span = _date_span(evidence, due)
        if span is None:
            continue  # 句子是真的，但里面没写这个日期
        if lower and upper and not lower <= due <= upper:
            continue
        due_time = _clock(clock)
        if due_time is False:
            continue
        if (due, due_time) in seen:
            continue
        seen.add((due, due_time))
        kind = item.get("label") if item.get("label") in KINDS else "其他"
        confidence = item.get("confidence") if item.get("confidence") in CONFIDENCES else "低"
        output.append(
            DeadlineCandidate(kind, due, due_time, _evidence(evidence, *span), confidence)
        )
    return output
