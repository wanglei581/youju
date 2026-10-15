"""从申报通知正文提取待人工核对的截止候选。规则宁可漏掉含糊表述。"""

import re
from dataclasses import dataclass
from datetime import date, time

from youju.text_lines import unwrap_layout_lines

_DATE = re.compile(
    r"(?:(?P<year>20\d{2})\s*[年./-]\s*)?"
    r"(?P<month>1[0-2]|0?[1-9])\s*[月./-]\s*"
    r"(?P<day>3[01]|[12]\d|0?[1-9])\s*日?"
)
_TIME = re.compile(r"(?P<hour>[01]?\d|2[0-3])\s*(?:[:：时])\s*(?P<minute>[0-5]\d)?\s*分?")
_SPLIT = re.compile(r"[。；;！？!?\n]+")
_ACTION = re.compile(r"申报|申请|推荐|审核|报送|提交|递交|受理|征集|遴选|认定|报名")
_CUE = re.compile(r"截止|截至|之前|前|止|期限|时间")
_EXCLUDE = re.compile(
    r"有效期|实施期限|执行期限|建设期限|项目周期|公示期|合同期限|政策期限|截至.{0,30}[，,]已(?:有|完成)"
)
_RECOMMEND = re.compile(r"推荐|审核|初审|市级|各(?:市|区|地).{0,25}(?:局|部门|委)|区县主管部门")
_MATERIAL = re.compile(r"材料|纸质|报送|寄送|邮寄|汇总|补交|补报")
_ENTERPRISE = re.compile(r"企业|单位|申报|申请|报名|受理|认定|遴选|征集|填报|提交")


@dataclass(frozen=True)
class DeadlineCandidate:
    kind: str
    due_date: date
    due_time: time | None
    evidence: str
    confidence: str


def same_moment(a_date: date, a_time: time | None, b_date: date, b_time: time | None) -> bool:
    """同一天，且两边都写了时刻时时刻也相同，算同一个截止（规则和 AI 的候选互相比对用）。"""
    return a_date == b_date and (a_time is None or b_time is None or a_time == b_time)


def _kind(context: str) -> str:
    if re.search(
        r"(?:申报|申请|报名)(?:时间|日期)?\s*截止|(?:网上|在线)(?:填报|提交)(?:时间|日期)?\s*(?:截止|截至)",
        context,
    ):
        return "企业申报截止"
    if _RECOMMEND.search(context):
        return "推荐或审核截止"
    if _MATERIAL.search(context):
        return "材料报送截止"
    if _ENTERPRISE.search(context):
        return "企业申报截止"
    return "其他"


def _evidence(sentence: str, date_start: int, date_end: int) -> str:
    if len(sentence) <= 120:
        return sentence
    desired = max(0, min(date_start - 40, len(sentence) - 120))
    if desired == 0:
        return sentence[:120]
    # 长句只能截取一段时，优先从分句开头或完整提示词开始。
    boundaries = [m.end() for m in re.finditer(r"[，,、。；;！？!?\n]", sentence[:date_start])]
    starts = [pos for pos in boundaries if desired <= pos <= date_start]
    start = starts[0] if starts else None
    if start is None:
        cues = [m.start() for m in _ACTION.finditer(sentence, desired, date_start)]
        start = cues[0] if cues else date_start
    start = min(start, date_end)
    return sentence[start : start + 120].strip("，,、 ")


def extract_deadlines(body: str, publish_date: date | None) -> list[DeadlineCandidate]:
    results: list[DeadlineCandidate] = []
    seen: set[tuple[str, date, time | None, str]] = set()
    # PDF 版面折行先接回整句，单独一行的"申报时间："这类标签也接上下一行的日期，否则证据句
    # 只剩半句（如"…报送至省工业和信息"）或只剩日期、看不出是什么时间（10/15 全面测试发现）
    body = unwrap_layout_lines(body, join_labels=True)
    for raw in _SPLIT.split(body):
        sentence = " ".join(raw.split()).strip("，, 、")
        if not sentence or len(sentence) > 500 or _EXCLUDE.search(sentence):
            continue
        if not _ACTION.search(sentence) or not _CUE.search(sentence):
            continue
        dates = list(_DATE.finditer(sentence))
        for match in dates:
            before = sentence[max(0, match.start() - 45) : match.start()]
            after = sentence[match.end() : match.end() + 35]
            # 区间起点后紧跟“至/到”，不作为截止。
            after_date = re.sub(r"^\s*[（(][^）)]{1,6}[）)]", "", after)
            if re.match(r"\s*(?:\d{1,2}\s*[:：时]\s*\d{0,2}\s*分?)?\s*[至到—–~-]", after_date):
                continue
            if re.match(r"\s*(?:以后|以来|之后|起)", after):
                continue
            nearby = before + match.group() + after
            # 时间段的结尾："至/到"，或前一个日期后面紧跟的短横线（"9 月 20 日-10 月 26 日"）
            range_end = bool(re.search(r"[至到]|日\s*[—–~-]\s*$", before[-20:])) and bool(
                re.search(r"(?:申报|提交|受理|填报|报名|征集)时间", sentence)
            )
            if not range_end and not (_ACTION.search(nearby) and _CUE.search(nearby)):
                continue
            if not (
                re.search(r"截止|截至|之前|前|止", nearby)
                or re.search(r"(?:申报|申请|受理|报名|征集|提交|报送)时间.{0,35}[至到]", before)
                or range_end
            ):
                continue
            year = int(match.group("year")) if match.group("year") else None
            month, day = int(match.group("month")), int(match.group("day"))
            previous_years = [
                int(item.group("year"))
                for item in dates
                if item.start() < match.start() and item.group("year")
            ]
            context_year = previous_years[-1] if previous_years else None
            if year is None and context_year is None and publish_date is None:
                continue
            try:
                due = date(year or context_year or publish_date.year, month, day)
                if year is None and context_year is None and due < publish_date:
                    due = date(publish_date.year + 1, month, day)
            except ValueError:
                continue
            if publish_date is not None and due < publish_date:
                continue
            tail = sentence[match.end() : match.end() + 20]
            tail = re.sub(r"^\s*[（(][^）)]{1,6}[）)]", "", tail)
            tail = re.sub(r"^\s*(?:上午|下午|中午|晚上)", "", tail)
            clock = _TIME.match(tail.lstrip(" ,，"))
            due_time = (
                time(int(clock.group("hour")), int(clock.group("minute") or 0)) if clock else None
            )
            if len(dates) == 1:
                context = sentence
            else:
                left = sentence[: match.start()]
                right = sentence[match.end() :]
                previous_comma = max(left.rfind("，"), left.rfind(","))
                next_comma = min(
                    (i for i in (right.find("，"), right.find(",")) if i >= 0), default=len(right)
                )
                context = left[previous_comma + 1 :] + match.group() + right[:next_comma]
                if not _ACTION.search(context) and next_comma < len(right):
                    context += right[next_comma + 1 : next_comma + 26]
            kind = _kind(context)
            if kind == "其他" and range_end:
                kind = _kind(sentence)
            confidence = (
                "高"
                if re.search(r"截止|截至", nearby) and kind != "其他"
                else "中"
                if kind != "其他"
                else "低"
            )
            evidence = _evidence(sentence, match.start(), match.end())
            key = (kind, due, due_time, evidence)
            if key not in seen:
                seen.add(key)
                results.append(DeadlineCandidate(kind, due, due_time, evidence, confidence))
    return results
