"""API 和后台任务共用的协议版本判断，不依赖 API 模块。"""

import re

VERSION_PREFIX = re.compile(r"\d{4}-\d{2}")


def agreement_at_least(version: str | None, needed: str) -> bool:
    """同意的版本不早于 needed（如 AI 条款第一次写进去的那一版）。写法不对的不算。"""
    return bool(version) and VERSION_PREFIX.match(version) is not None and version >= needed
