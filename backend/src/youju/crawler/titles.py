"""列表标题的装饰前缀清理，也供历史题录重算使用。"""

import re


def clean_title(value: str) -> str:
    # 宁夏列表链接里的“》”是项目装饰符，text_content 会带入标题；只剥离开头孤立闭书名号。
    # 正常《…》以及正文中间的书名号保留，避免损坏正式文件名称。
    return re.sub(r"^(?:》\s*)+", "", value.strip())
