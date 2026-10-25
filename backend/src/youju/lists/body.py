"""名单类通知的正文在站内怎么给（D-092，创始人 10/25）：通知文字照常可读，正文里的名单不展开。

正文只存纯文字（crawler/detail.py 的 _plain_lines）：网页表格的一行常常变成一行文字、格子之间只剩
空格，格子里套了段落的又是一个格子一行，长名称还会被折成两行。分不出哪几行原来是表格，所以按"整块"
挡，宁可多挡：

1. 先认出名单里的行：
   - 可靠的：有一段（按空格、顿号、逗号、分号分开）正好是这份通知解析出的条目名称（不长的行、
     序号开头的行，带标点也算）；一个格子一行的表格里只有序号的行（1、2、3……逐个递增）；
     逐行编号的条目连着 5 行以上、编号逐个递增（"1、北京市朝阳区""2、天津市武清区"……，每行
     不超过 40 个字、没有句子标点，"1、申报条件"这类小标题不算）；多列对齐的表格行连着 4 行以上
     （每行按空格分成的段数一样、至少 3 段）。有条目名称、但带"请、如有、异议、反映、特此"的
     句子不算。
   - 较弱的（行里没有句子标点 ，。；：？！，也不是日期；序号开头、三段以上的表格行带日期也算）：
     只有表头用词（序号、企业名称、姓名……，字间带空格的也算）；
     三段以上空格分开的短词、其中有单位名或人名；第一段只是序号、后面还有别的段；整行只是一个单位名
     或像一个人名（常见姓氏开头，和正文脱敏认姓名同一套）；两个以上像人名的词并列。较弱的要成片出现
     （前后 6 行内还有一行）才算数：正文里偶尔一行被认错不会把前后的通知文字一起挡掉。
   发文机关的署名行（归并组里各版本的来源部门全称、发文机关）和称呼（"各有关企业"）都不算。
2. 从第一行到最后一行算数的行，连同中间的序号、类别、被折开的半个名称，整块换成一行说明
   （PLACEHOLDER）；起点前面紧挨着的表头格子（每行不超过 8 个字，不算空格）、终点后面紧跟的几行
   最后一家的格子（不超过 12 个字的短词，或 30 字以内的半个名称）也算进去，含"请、如有、异议、
   公示、特此"这类通知用语的不算。这一块前后的通知文字（公示期、异议方式、署名、日期）照常显示。
3. 不放行的情况（整篇不展示正文，Filtered.reason 说为什么）：挡了名单、剩下的通知文字不到
   MIN_CHARS 字（list，正文主要是名单）；挡完以后留下的文字里还有 LEAK_NAMES 家以上名单里的单位
   名称（按名称开头几个字找，mixed：有名单没认出来，宁可不给）。没认出名单、正文本来就不到
   MIN_CHARS 字的（多半没抓全，或只抓到网页导航）给 short，按普通正文没抓全处理。

失信、处罚类和人才、专家等个人类名单（rules.excluded_list_title）不进查名单，正文照旧不展示
（readable 给 False）。名单附件不在这里：附件照旧只给查询结果（D-088）。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.crawler.detail import _name_like
from youju.lists.rules import excluded_list_title, normalize_name, valid_org_name

PLACEHOLDER = "〔这里是名单，站内不展开，可以在查名单里查企业〕"
# 挡完名单还剩这么多字的通知文字才给读：公示通知的正文常常只有一两百字（公示期、异议方式），
# 不能用普通正文的下限（v1.BODY_MIN_CHARS）把它们一起挡掉
MIN_CHARS = 60
# 挡完以后留下的文字里还有这么多家名单里的单位名称（按名称的前 LEAK_PREFIX 个字找：PDF 转成的表格
# 常把名称拆到几行），就算没挡住、整篇不给（通知原句里一般只点一家："拟认定××公司等 64 家企业"）
LEAK_NAMES = 3
LEAK_PREFIX = 8
# 句子标点：有这些的行是通知里的话。逗号、冒号后面紧跟数字的不算（"4,300""9:00""9：00"
# "统一代码：9137……"）
_SENTENCE = re.compile(r"[，。；？！]|：(?!\d)|[,;:?!](?!\d)")
_PARTS = re.compile(r"[\s、，；;/／]+|,(?!\d)")
# 序号：1、（2）、3.、4,300（千分位）
_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d{1,6})"
_SEQUENCE = re.compile(rf"[(（]?{_NUMBER}[)）.、．]?")
_LEADING_SEQUENCE = re.compile(rf"^[(（]?{_NUMBER}[)）.、．]?\s*")
_ISSUER_SPLIT = re.compile(r"[\s、，,；;/]+")
_DATE = re.compile(r"\d{4}\s*年|\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}[-./]\d{1,2}[-./]\d{1,2}")
# 有条目名称的行多长以内、带标点也算名单里的行（更长的是一句话："拟认定××公司等 64 家企业……"）
_ENTRY_LINE_MAX = 80
# 表格行里一段的长度上限（超过的多半是一句话）
_CELL_MAX = 30
# 较弱的行要成片出现：前后这么多行内还有一行（一个格子一行的表格每行有几列，名称就隔几行）
_NEAR = 6
# 逐行编号的条目（"1、北京市朝阳区"）：连着多少行算名单、每行最多几个字
_NUMBERED_ITEM = re.compile(rf"^[(（]?({_NUMBER})[)）.、．]\s*\S")
_RUN_MIN, _ITEM_MAX = 5, 40
# 起点前面算进去的表头格子、终点后面算进去的短行：最多几行、每行最多几个字（不算空格：PDF 转成的
# 文字常常字字之间带空格，"序 号 集 群 名 称"）
_HEAD_LINES, _HEAD_MAX = 8, 8
_TAIL_LINES, _TAIL_MAX = 8, 12
# 称呼（"各有关企业""有关单位"）不是名单
_GREETING = ("各", "有关", "相关", "全体", "广大", "以下", "下列")
# 结尾这样写的不是名单（"特此公示"）
_CLOSING = ("特此", "此复", "此件")
# 名单最后一家后面紧跟的行里有这些通知用语的，不并进名单那一块（"如有异议请向市科委反映"）
_NOTICE_WORDS = (
    "请",
    "如有",
    "异议",
    "反映",
    "联系",
    "电话",
    "公示",
    "公告",
    "特此",
    "附件",
    "根据",
    "按照",
)
# 有条目名称、但带着这些话的是通知里的句子，不算名单里的行
# （"1 某某公司 须于 10 月 26 日前提异议。"）
_OBJECTION_WORDS = ("请", "如有", "异议", "反映", "特此")
# 逐行编号的通知小标题常用词（"1、申报条件""2、材料要求"）：带这些词、又不是单位名的不算名单条目
_OUTLINE_WORDS = ("条件", "要求", "材料", "事项", "程序", "办法", "流程", "安排")
# 多列对齐的表格行（格子之间只剩空格）：连着这么多行、每行段数一样（至少 3 段）就算名单
_ALIGNED_MIN, _ALIGNED_COLUMNS = 4, 3
# 被折开的半个名称（"某某（天津）网络安全科技有限责任公"）
_NAME_PIECE = re.compile(r"有限|股份|公司|集团|科技|研究|技术|中心")
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
        "姓名",
        "性别",
        "手机",
        "电话",
        "联系电话",
        "身份证号",
        "职务",
        "职称",
        "工作单位",
        "联系人",
        "负责人",
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


def _org_or_person(value: str) -> bool:
    bare = _bare(value)
    return bool(bare) and (valid_org_name(bare) or _name_like(bare) or bare in HEADER_WORDS)


_HEADER_WORD_MAX = max(len(word) for word in HEADER_WORDS)


def _header_only(value: str) -> bool:
    """去掉空格后整行能拆成表头用词（"序号 企业名称""序 号 姓 名 工 作 单 位 职 务"）。"""
    compact = re.sub(r"\s+", "", value)
    if not compact or len(compact) > 60:
        return False
    ok = [True] + [False] * len(compact)
    for end in range(1, len(compact) + 1):
        start = max(0, end - _HEADER_WORD_MAX)
        ok[end] = any(ok[i] and compact[i:end] in HEADER_WORDS for i in range(start, end))
    return ok[-1]


def line_kind(line: str, names: set[str], keep: set[str]) -> str | None:
    """这一行是不是名单里的（规则见模块说明第 1 条）：entry 有条目名称，weak 较弱的，不是的
    给 None。"""
    stripped = line.strip()
    if not stripped or _signature(stripped, keep):
        return None
    sentence = _SENTENCE.search(stripped) is not None
    tokens = stripped.split()
    parts = [p for p in _PARTS.split(stripped) if p]
    hits = sum(1 for part in parts if _bare(part) in names)
    seq_first = _SEQUENCE.fullmatch(tokens[0]) is not None
    objection = any(word in stripped for word in _OBJECTION_WORDS)
    if hits and (
        not sentence
        or (not objection and (hits >= 2 or len(stripped) <= _ENTRY_LINE_MAX or seq_first))
    ):
        return "entry"
    if sentence or stripped.startswith(_CLOSING):
        return None
    if _DATE.search(stripped):
        # 带日期的只有表格行（序号开头、三段以上）才算："86 某某团队 2024年5月 创业组 芜湖市"；
        # "2024年10月1日 至 2024年10月7日"、署名日期这类照常显示
        # （以日期开头的不算：PDF 转出的"2026 年 7 月 15 日"字间有空格，年份像序号）
        table_row = seq_first and len(tokens) >= 3 and _DATE.match(stripped) is None
        return "weak" if table_row else None
    if _header_only(stripped):
        return "weak"
    if len(tokens) >= 2 and seq_first:
        return "weak"
    if (
        len(tokens) >= 3
        and all(len(t) <= _CELL_MAX for t in tokens)
        and any(_org_or_person(t) for t in tokens)
    ):
        return "weak"
    if sum(1 for part in parts if (bare := _bare(part)) and _name_like(bare)) >= 2:
        return "weak"
    whole = _bare(stripped)
    if not whole or whole.startswith(_GREETING):
        return None
    return "weak" if valid_org_name(whole) or _name_like(whole) else None


def is_list_line(line: str, names: set[str], keep: set[str]) -> bool:
    return line_kind(line, names, keep) is not None


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


def _numbered_runs(lines: list[str]) -> set[int]:
    """逐行编号的名单：连着 _RUN_MIN 行以上、编号逐个递增、每行都是不带句子标点的短条目。中间可以
    夹一行被折开的后半截（不带句子标点的短行）。通知里的小标题（"1、申报条件"）中间隔着段落，
    连不成片。"""
    marked: set[int] = set()
    run: list[tuple[int, int]] = []
    gap = 0

    def close() -> None:
        if len(run) >= _RUN_MIN:
            marked.update(i for i, _ in run)

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        match = _NUMBERED_ITEM.match(stripped)
        item = (
            match is not None
            and len(stripped) <= _ITEM_MAX
            and not _SENTENCE.search(stripped)
            and not _DATE.search(stripped)
            and (
                not any(word in stripped for word in _OUTLINE_WORDS)
                or valid_org_name(_bare(stripped))
            )
        )
        if item:
            value = int(match.group(1).replace(",", ""))
            if run and value == run[-1][1] + 1:
                run.append((i, value))
            else:
                close()
                run = [(i, value)]
            gap = 0
        elif run and gap == 0 and len(stripped) <= _CELL_MAX and not _SENTENCE.search(stripped):
            gap = 1
        else:
            close()
            run, gap = [], 0
    close()
    return marked


def _aligned_runs(lines: list[str]) -> set[int]:
    """多列对齐的表格行：连着 _ALIGNED_MIN 行以上（中间可以有空行），每行按空格分成的段数一样、
    至少 _ALIGNED_COLUMNS 段，每段不超过 _CELL_MAX 字，没有句子标点和日期。没解析出条目名称、
    名称也不像单位名的表格（"柔化科技团队 创业组 芜湖市"）靠这个认出来；通知里的话不会连着
    几行段数一样。"""
    marked: set[int] = set()
    run: list[int] = []
    width = 0

    def close() -> None:
        if len(run) >= _ALIGNED_MIN:
            marked.update(run)

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        tokens = stripped.split()
        row = (
            len(tokens) >= _ALIGNED_COLUMNS
            and all(len(token) <= _CELL_MAX for token in tokens)
            and not _SENTENCE.search(stripped)
            and not _DATE.search(stripped)
        )
        if row and run and len(tokens) == width:
            run.append(i)
            continue
        close()
        run, width = ([i], len(tokens)) if row else ([], 0)
    close()
    return marked


def _compact_len(value: str) -> int:
    return len(re.sub(r"\s+", "", value))


def _short(line: str, keep: set[str], limit: int) -> bool:
    stripped = line.strip()
    return (
        bool(stripped)
        and _compact_len(stripped) <= limit
        and not _SENTENCE.search(stripped)
        and not _DATE.search(stripped)
        and not stripped.startswith(_CLOSING)
        and not _signature(stripped, keep)
    )


def _tail(line: str, keep: set[str]) -> bool:
    """名单最后一家后面紧跟的这一行还算不算这一家的格子：短词，或被折开的半个名称。"""
    stripped = line.strip()
    if not _short(line, keep, _CELL_MAX) or any(word in stripped for word in _NOTICE_WORDS):
        return False
    return _compact_len(stripped) <= _TAIL_MAX or _NAME_PIECE.search(stripped) is not None


@dataclass(frozen=True)
class Filtered:
    """过滤的结果：text 是给读的文字（整篇不给时为 None），hidden 是有没有挡掉名单；整篇不给时
    reason 是 list、mixed 或 short（见模块说明第 3 条）。"""

    text: str | None
    hidden: bool = False
    reason: str | None = None


def filter_text(text: str, *, names: set[str], keep: set[str], min_chars: int) -> Filtered:
    """把名单那一块换成一行说明（规则见模块说明）。"""
    lines = (text or "").split("\n")
    kinds = {i: kind for i, line in enumerate(lines) if (kind := line_kind(line, names, keep))}
    for i in _sequence_runs(lines) | _numbered_runs(lines) | _aligned_runs(lines):
        kinds[i] = "seq"
    found = sorted(kinds)
    marks = [
        i
        for k, i in enumerate(found)
        if kinds[i] != "weak"
        or (k > 0 and i - found[k - 1] <= _NEAR)
        or (k + 1 < len(found) and found[k + 1] - i <= _NEAR)
    ]
    hidden = bool(marks)
    if not marks:
        kept = lines
    else:
        first, last = marks[0], marks[-1]
        head = 0
        while (
            first > 0
            and head < _HEAD_LINES
            and (_short(lines[first - 1], keep, _HEAD_MAX) or _header_only(lines[first - 1]))
        ):
            first -= 1
            head += 1
        tail = 0
        while last + 1 < len(lines) and tail < _TAIL_LINES and _tail(lines[last + 1], keep):
            last += 1
            tail += 1
        kept = [*lines[:first], PLACEHOLDER, *lines[last + 1 :]]
    visible_text = "\n".join(line for line in kept if line != PLACEHOLDER)
    if _compact_len(visible_text) < min_chars:
        return Filtered(None, hidden, "list" if hidden else "short")
    # 兜底：留下的文字里还有好几家名单里的单位，说明有名单没认出来
    compact = normalize_name(visible_text)
    if names and sum(1 for name in names if name[:LEAK_PREFIX] in compact) >= LEAK_NAMES:
        return Filtered(None, hidden, "mixed")
    return Filtered("\n".join(kept), hidden)


def _issuers(values: Iterable[str | None]) -> set[str]:
    keep: set[str] = set()
    for value in values:
        for part in _ISSUER_SPLIT.split(value or ""):
            if part := normalize_name(part):
                keep.add(part)
    return keep


def filter_for(
    session: Session, doc_ids: list[int], *, min_chars: int
) -> Callable[[str], Filtered]:
    """给一份名单类通知（归并组 doc_ids）做好过滤函数：用这一组解析出的条目名称，和这一组各版本的
    来源部门全称、发文机关当署名（正文可能取自组里另一个版本）。"""
    from youju.models import Document, DocumentDetail, ListEntry, Source

    names = {
        name
        for (name,) in session.execute(
            select(ListEntry.normalized_name).where(ListEntry.document_id.in_(doc_ids)).distinct()
        )
        if name
    }
    issuers = [
        value
        for row in session.execute(
            select(Source.dept_full_name, DocumentDetail.issuer)
            .select_from(Document)
            .join(Source, Source.id == Document.source_id)
            .outerjoin(DocumentDetail, DocumentDetail.document_id == Document.id)
            .where(Document.id.in_(doc_ids))
        )
        for value in row
    ]
    keep = _issuers(issuers)

    def apply(text: str) -> Filtered:
        return filter_text(text, names=names, keep=keep, min_chars=min_chars)

    return apply
