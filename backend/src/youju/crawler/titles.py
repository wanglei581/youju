"""列表标题的清理：装饰前缀、粘在标题后面的正文开头。也供历史题录重算使用。"""

import re
from collections.abc import Iterable

# 有的列表（如上海市经信委）把正文开头也放进了链接文字：标题后面跟着空白，再跟称呼
# （"有关单位："）或正文第一句（"为规范……""根据……"）。只在"文种词 + 空白 + 称呼或正文起句"
# 时剪掉后面；其余带空格的标题不动（几个机关联合发文的"某厅 某厅 关于……"、"……的通知 （第二批）"）。
_DOC_TYPE = r"(?:通知|公告|通告|公示|函)"
_SALUTATION = r"(?:各|有关|相关)[^：:\s]{0,24}[：:]"
_BODY_START = r"(?:为|根据|按照|依据|现将|经)\S{8,}"
_TRAILING_BODY = re.compile(
    rf"^(.{{6,200}}?{_DOC_TYPE})\s+(?:{_SALUTATION}|{_BODY_START}).*$", re.S
)

# 列表页截断长标题时加的省略号："..."（有的多打一个点）或"…"
# （10/16 验收：天津、青海、河南、湖南、宁夏、重庆）
_ELLIPSIS = ("...", "…")
# 去掉省略号后至少剩这么多字才找完整标题：太短的开头（如"关于印发《"）容易撞上页面里别的标题
_MIN_PREFIX = 10
# 完整标题的长度上限：本机库最长的正常标题 134 字；更长的多半是把正文或整块内容当成了标题
_MAX_TITLE = 200
# 详情页标题里的网页标记残留（如辽宁工信厅 ArticleTitle 里的"&ltbr>"），这样的写法不采用
_MARKUP_RESIDUE = re.compile(r"[<>]|&(?:lt|gt|amp|nbsp|quot)", re.I)


def clean_title(value: str) -> str:
    # 宁夏列表链接里的“》”是项目装饰符，text_content 会带入标题；只剥离开头孤立闭书名号。
    # 正常《…》以及正文中间的书名号保留，避免损坏正式文件名称。
    title = re.sub(r"^(?:》\s*)+", "", value.strip())
    match = _TRAILING_BODY.match(title)
    return match.group(1) if match else title


def is_truncated(title: str) -> bool:
    return title.rstrip().endswith(_ELLIPSIS)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def completed_title(stored: str, page_titles: Iterable[str]) -> str | None:
    """列表页截断的标题，用详情页自己标明的标题（ArticleTitle 元数据、h1）补全；对不上返回 None。

    不猜：去掉全部空白后，详情页标题必须以已存标题去掉省略号的部分开头、比它长、本身不以省略号结尾、
    不带网页标记残留。符合条件的只有一种写法才采用；元数据和 h1 写得不一样时不改。"""
    base = clean_title(stored)
    if not is_truncated(base):
        return None
    prefix = _compact(base).rstrip(".…")
    if len(prefix) < _MIN_PREFIX:
        return None
    found: dict[str, str] = {}
    for raw in page_titles:
        title = clean_title(" ".join(raw.split()))
        compact = _compact(title)
        if (
            compact.startswith(prefix)
            and len(prefix) < len(compact) <= _MAX_TITLE
            and not is_truncated(title)
            and not _MARKUP_RESIDUE.search(title)
        ):
            found.setdefault(compact, title)
    return next(iter(found.values())) if len(found) == 1 else None
