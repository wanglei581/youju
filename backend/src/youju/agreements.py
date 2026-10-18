"""API 和后台任务共用的协议版本判断，不依赖 API 模块。"""

import re

VERSION_PREFIX = re.compile(r"\d{4}-\d{2}")

# 加量体验（隐私保护指引一·15，含注销后的核对编号）写进指引的那一版。协议升版过渡期
# （YOUJU_AGREEMENT_MIN_VERSION 填旧版本）旧的同意照旧能用关注、纠错，但没告知过体验记录，
# 所以开始体验时另外核对；固定写在这里，回退配置也不放宽（同 AI 的 AI_AGREEMENT_VERSION）
TRIAL_AGREEMENT_VERSION = "2026-11"


def agreement_at_least(version: str | None, needed: str) -> bool:
    """同意的版本不早于 needed（如 AI 条款第一次写进去的那一版）。写法不对的不算。"""
    return bool(version) and VERSION_PREFIX.match(version) is not None and version >= needed
