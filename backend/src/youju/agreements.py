"""API 和后台任务共用的协议版本判断，不依赖 API 模块。"""

import re

VERSION_PREFIX = re.compile(r"\d{4}-\d{2}")

# 批量工具的任务记录和次数（隐私保护指引一·13）、加量体验（一·15）写进指引的那一版。
# 协议升版过渡期（YOUJU_AGREEMENT_MIN_VERSION 填旧版本）旧的同意照旧能用关注、纠错，
# 但没告知过这些，所以同意工具使用说明、提交名单、开始加量体验时另外核对；
# 固定写在这里，回退配置也不放宽（同 AI 的 AI_AGREEMENT_VERSION）
TOOLS_AGREEMENT_VERSION = "2026-11"


def agreement_at_least(version: str | None, needed: str) -> bool:
    """同意的版本不早于 needed（如 AI 条款第一次写进去的那一版）。写法不对的不算。"""
    return bool(version) and VERSION_PREFIX.match(version) is not None and version >= needed
