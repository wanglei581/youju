"""列表标题的清理：装饰前缀、粘在标题后面的正文开头。也供历史题录重算使用。"""

import re

# 有的列表（如上海市经信委）把正文开头也放进了链接文字：标题后面跟着空白，再跟称呼
# （"有关单位："）或正文第一句（"为规范……""根据……"）。只在"文种词 + 空白 + 称呼或正文起句"
# 时剪掉后面；其余带空格的标题不动（几个机关联合发文的"某厅 某厅 关于……"、"……的通知 （第二批）"）。
_DOC_TYPE = r"(?:通知|公告|通告|公示|函)"
_SALUTATION = r"(?:各|有关|相关)[^：:\s]{0,24}[：:]"
_BODY_START = r"(?:为|根据|按照|依据|现将|经)\S{8,}"
_TRAILING_BODY = re.compile(
    rf"^(.{{6,200}}?{_DOC_TYPE})\s+(?:{_SALUTATION}|{_BODY_START}).*$", re.S
)


def clean_title(value: str) -> str:
    # 宁夏列表链接里的“》”是项目装饰符，text_content 会带入标题；只剥离开头孤立闭书名号。
    # 正常《…》以及正文中间的书名号保留，避免损坏正式文件名称。
    title = re.sub(r"^(?:》\s*)+", "", value.strip())
    match = _TRAILING_BODY.match(title)
    return match.group(1) if match else title
