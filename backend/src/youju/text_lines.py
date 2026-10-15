"""PDF 转出的正文按版面换行，一句话常被拆在几行里。

AI 解读核对引用、截止日期抽取证据句都要先接回整句。

判断版面换行不靠猜标题：PDF 里被折行的那一行几乎排满版面，标题和段落末行短得多。
只有上一行没有句末标点、长度接近这份正文常见的整行宽度、中间没有空行、下一行也不以编号开头时，
才算同一句。网页转出来的正文一段一行、长短不一，基本不会触发。
"""

import re

SENTENCE_END = re.compile(r"[。！？!?；;][\"'”’）)]*\s*$")
ITEM_START = re.compile(
    r"\s*(?:[一二三四五六七八九十百]+[、．.]|[（(][一二三四五六七八九十百\d]+[）)]"
    r"|\d+(?:\.\d+)*[、．.）)](?!\d)|\d+(?:\.\d+)+\s+(?![万亿千百元个项人家年月日%倍以及左余多岁天米吨])"
    r"|[①-⑳]|[A-Za-z][、．.）)](?![A-Za-z0-9])|第[一二三四五六七八九十百\d]+[条章节项部分]|附件)"
)
# 以文件名结尾的长行是标题（如"关于组织开展……工作的通知"），排满一行也不接下一段
TITLE_END = re.compile(r"(?:通知|公告|通告|指南|办法|细则|方案|意见|规定|决定|批复|函)\s*$")
FULL_LINE = 0.85  # 上一行至少达到常见整行宽度的这个比例，才算被版面折行
MIN_WIDTH = 15  # 常见整行宽度低于这个字数（短行为主，如表格）时不接
# 单独一行的时间标签（如"（二）申报时间："），日期写在下一行；只认时间、日期、期限、截止这类标签，
# "各有关单位："这类称呼不接
LABEL_END = re.compile(r"(?:时间|日期|期限|截止|截至)[^：:]{0,6}[：:]\s*$")
LABEL_MAX = 30


def line_width(body: str) -> int:
    """这份正文常见的整行宽度：较长的那四分之一行的长度（PDF 折行的整行占多数）。"""
    lengths = sorted(len(line.strip()) for line in body.split("\n") if line.strip())
    if len(lengths) < 4:
        return 0
    return lengths[(len(lengths) * 3) // 4]


def layout_wrapped(line: str, next_line: str, width: int) -> bool:
    """line 到 next_line 之间的换行是版面折行。

    没有句末标点、不是文件名结尾、接近整行宽度、下一行不是编号。"""
    line = line.strip()
    if width < MIN_WIDTH or not line or not next_line.strip():
        return False
    if SENTENCE_END.search(line) or TITLE_END.search(line) or len(line) < width * FULL_LINE:
        return False
    return not ITEM_START.match(next_line)


def unwrap_layout_lines(body: str, *, join_labels: bool = False) -> str:
    """去掉版面折行处的换行（同一句接回一行）；join_labels 时，单独一行的短标签也接上下一行的值。

    只删换行、不改其他字符，空行和编号开头的行照旧分开。"""
    width = line_width(body)
    lines = body.split("\n")
    out = [lines[0]]
    physical = lines[0]  # 上一条物理行（判断折行只看它，不看已接起来的整句）
    for line in lines[1:]:
        label = (
            join_labels
            and LABEL_END.search(physical.strip())
            and 0 < len(physical.strip()) <= LABEL_MAX
            and line.strip()
            and not ITEM_START.match(line)
        )
        if layout_wrapped(physical, line, width) or label:
            out[-1] = out[-1].rstrip() + line.lstrip()
        else:
            out.append(line)
        physical = line
    return "\n".join(out)
