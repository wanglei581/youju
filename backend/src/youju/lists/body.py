"""名单类通知的正文在站内怎么给（D-092，创始人 10/25）：通知文字照常可读，正文里的名单不展开。

正文只存纯文字（crawler/detail.py 的 _plain_lines）：网页表格的一行常常变成一行文字、格子之间只剩
空格，格子里套了段落的又是一个格子一行，长名称还会被折成两行。分不出哪几行原来是表格，所以按"整块"
挡，宁可多挡：

1. 先认"像名单的行"（没有句子标点 ，。；：？！ 的行里）：有一段（按空格、顿号、逗号、分号分开）正好
   是这份通知解析出的名单条目名称；只有表头用词（序号、企业名称、所在地区……）；三段以上空格分开的
   短词；第一段只是序号、后面还有别的段；整行（去掉开头的序号）只是一个单位名，或像一个人名（常见
   姓氏开头，和正文脱敏认姓名同一套）。发文机关的署名行（来源的部门全称、详情页认出的发文机关）不算。
2. 从第一行到最后一行像名单的行，连同中间的序号、类别、被折开的半个名称，整块换成一行说明
   （PLACEHOLDER）。一个格子一行的表格，只有序号的行（1、2、3……逐个递增）也算像名单的行。
   起点要么是有条目名称的行，要么成片出现（前后 6 行内还有一行像名单的）：正文里偶尔一行被认错
   （一个单位名、像姓名的两个字）不会把后面的通知文字一起挡掉，称呼"各有关企业"也不算。起点前面
   紧挨着的表头格子（每行不超过 8 个字）、终点后面紧跟的不长又没有标点的几行（最后一家的类别、
   半个名称）也算进去。这一块前后的通知文字（公示期、异议方式、署名、日期）照常显示。
3. 挡完剩下的通知文字不到 MIN_CHARS 字的，整篇按名单处理、不展示正文。

失信、处罚类和人才、专家等个人类名单（rules.excluded_list_title）不进查名单，正文照旧不展示
（readable 给 False）。名单附件不在这里：附件照旧只给查询结果（D-088）。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.crawler.detail import _name_like
from youju.lists.rules import excluded_list_title, normalize_name, valid_org_name

PLACEHOLDER = "〔这里是名单，站内不展开，可以在查名单里查企业〕"
# 挡完名单还剩这么多字的通知文字才给读：公示通知的正文常常只有一两百字（公示期、异议方式），
# 不能用普通正文的下限（v1.BODY_MIN_CHARS）把它们一起挡掉
MIN_CHARS = 60
# 句子标点：有这些的行是通知里的话，照常显示。半角逗号、冒号后面紧跟数字的不算（"4,300""9:00"）
_SENTENCE = re.compile(r"[，。；：？！]|[,;:?!](?!\d)")
_PARTS = re.compile(r"[\s、，；;]+|,(?!\d)")
# 序号：1、（2）、3.、4,300（千分位）
_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d{1,6})"
_SEQUENCE = re.compile(rf"[(（]?{_NUMBER}[)）.、．]?")
_LEADING_SEQUENCE = re.compile(rf"^[(（]?{_NUMBER}[)）.、．]?\s*")
_ISSUER_SPLIT = re.compile(r"[\s、，,；;/]+")
_DATE = re.compile(r"\d{4}\s*年|\d{1,2}\s*月\s*\d{1,2}\s*日")
# 表格行里一段的长度上限（超过的多半是一句话）
_CELL_MAX = 30
# 名单表格最后一行后面还算进这一块的短行（类别、半个名称）最多几行；第一行前面紧挨着的表头
# 格子（一个格子一行的表格：序号、地址、申请名称……）算进去的最多几行、每行最多几个字
_TAIL_LINES = 8
_HEAD_LINES = 8
_HEAD_MAX = 8
# 像名单的行要成片出现（前后这么多行内还有一行）才算名单那一块的起点：正文里偶尔一行被认错
# （一个单位名、像姓名的两个字）不会把后面的通知文字一起挡掉。一个格子一行的表格每行有几列，
# 名称就隔几行，这里放宽到 6
_NEAR = 6
# 称呼（"各有关企业""有关单位"）不是名单
_GREETING = ("各", "有关", "相关", "全体", "广大", "以下", "下列")
# 名单表头常用词：整行只有这些词的是表头
HEADER_WORDS = frozenset(
    {
        "序号",
        "编号",
        "名称",
        "企业名称",
        "单位名称",
        "机构名称",
        "企业",
        "单位",
        "申报单位",
        "承担单位",
        "推荐单位",
        "推荐部门",
        "主管部门",
        "推荐区",
        "所属区",
        "所在区",
        "区县",
        "地区",
        "所在地区",
        "所属地区",
        "地市",
        "所在地市",
        "市州",
        "统一社会信用代码",
        "信用代码",
        "项目名称",
        "类别",
        "类型",
        "领域",
        "行业",
        "批次",
        "备注",
        "证书编号",
        "得分",
        "排名",
        "名次",
    }
)


def readable(title: str | None) -> bool:
    """这份名单类通知的正文能不能在站内读：失信、处罚类和个人类名单不能。"""
    return not excluded_list_title(title or "")


def _bare(value: str) -> str:
    return normalize_name(_LEADING_SEQUENCE.sub("", value))


def _signature(line: str, keep: set[str]) -> bool:
    parts = [p for p in _ISSUER_SPLIT.split(line.strip()) if p]
    return bool(parts) and all(normalize_name(p) in keep for p in parts)


def line_kind(line: str, names: set[str], keep: set[str]) -> str | None:
    """这一行像不像名单（规则见模块说明第 1 条）：entry 是有一段正好是条目名称（最可靠），
    weak 是别的几条，不像给 None。"""
    stripped = line.strip()
    if not stripped or _SENTENCE.search(stripped) or _signature(stripped, keep):
        return None
    if any(_bare(part) in names for part in _PARTS.split(stripped) if part):
        return "entry"
    tokens = stripped.split()
    if all(token in HEADER_WORDS for token in tokens):
        return "weak"
    if len(tokens) >= 3 and all(len(t) <= _CELL_MAX for t in tokens):
        return "weak"
    if len(tokens) >= 2 and _SEQUENCE.fullmatch(tokens[0]):
        return "weak"
    whole = _bare(stripped)
    if not whole or whole.startswith(_GREETING):
        return None
    return "weak" if valid_org_name(whole) or _name_like(whole) else None


def is_list_line(line: str, names: set[str], keep: set[str]) -> bool:
    return line_kind(line, names, keep) is not None


def _tail(line: str, keep: set[str]) -> bool:
    stripped = line.strip()
    return (
        bool(stripped)
        and len(stripped) <= _CELL_MAX
        and not _SENTENCE.search(stripped)
        and not _DATE.search(stripped)
        and not _signature(stripped, keep)
    )


def _number(line: str) -> int | None:
    stripped = line.strip()
    if re.fullmatch(_NUMBER, stripped):
        return int(stripped.replace(",", ""))
    return None


def _sequence_runs(lines: list[str]) -> set[int]:
    """一个格子一行的表格里只有序号的行：1、2、3……逐个递增（下一个在 10 行以内）的都算。
    名单里的名称认不出来（没解析出条目、不是常见的单位名）时，靠这个认出表格。"""
    numbers = [(i, v) for i, line in enumerate(lines) if (v := _number(line)) is not None]
    marked: set[int] = set()
    for k, (i, v) in enumerate(numbers):
        for j, w in numbers[k + 1 :]:
            if j - i > 10:
                break
            if w == v + 1:
                marked.update((i, j))
                break
    return marked


def _head(line: str, keep: set[str]) -> bool:
    stripped = line.strip()
    return len(stripped) <= _HEAD_MAX and _tail(line, keep)


def filter_text(text: str, *, names: set[str], keep: set[str], min_chars: int) -> str | None:
    """把名单那一块换成一行说明；剩下的通知文字不到 min_chars 字时给 None（整篇按名单处理）。"""
    lines = (text or "").split("\n")
    kinds = {i: kind for i, line in enumerate(lines) if (kind := line_kind(line, names, keep))}
    for i in _sequence_runs(lines):
        kinds.setdefault(i, "weak")
    found = sorted(kinds)
    # 块的起点：有条目名称的行，或者成片出现的（前后 _NEAR 行内还有一行像名单的）；终点：最后一行
    # 像名单的，再把紧跟着的短行（序号、类别、被折开的半个名称，最多 _TAIL_LINES 行）算进去
    starts = [
        i
        for k, i in enumerate(found)
        if kinds[i] == "entry"
        or (k > 0 and i - found[k - 1] <= _NEAR)
        or (k + 1 < len(found) and found[k + 1] - i <= _NEAR)
    ]
    if not starts:
        kept = lines
    else:
        first, last = starts[0], found[-1]
        head = 0
        while first > 0 and head < _HEAD_LINES and _head(lines[first - 1], keep):
            first -= 1
            head += 1
        tail = 0
        while last + 1 < len(lines) and tail < _TAIL_LINES and _tail(lines[last + 1], keep):
            last += 1
            tail += 1
        kept = [*lines[:first], PLACEHOLDER, *lines[last + 1 :]]
    visible = sum(len(line.strip()) for line in kept if line != PLACEHOLDER)
    if visible < min_chars:
        return None
    return "\n".join(kept)


def _issuers(values: Iterable[str | None]) -> set[str]:
    keep: set[str] = set()
    for value in values:
        for part in _ISSUER_SPLIT.split(value or ""):
            if part := normalize_name(part):
                keep.add(part)
    return keep


def filter_for(
    session: Session, doc_ids: list[int], issuers: Iterable[str | None], *, min_chars: int
) -> Callable[[str], str | None]:
    """给一份名单类通知（归并组 doc_ids）做好过滤函数：用这一组解析出的条目名称、发文机关署名。"""
    from youju.models import ListEntry

    names = {
        name
        for (name,) in session.execute(
            select(ListEntry.normalized_name).where(ListEntry.document_id.in_(doc_ids)).distinct()
        )
        if name
    }
    keep = _issuers(issuers)

    def apply(text: str) -> str | None:
        return filter_text(text, names=names, keep=keep, min_chars=min_chars)

    return apply
