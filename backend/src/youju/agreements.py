"""API 和后台任务共用的协议版本判断，不依赖 API 模块。"""

import re

VERSION_PREFIX = re.compile(r"\d{4}-\d{2}")

# 批量工具的任务记录和次数（隐私保护指引一·13）、加量体验（一·15）写进指引的那一版。
# 协议升版过渡期（YOUJU_AGREEMENT_MIN_VERSION 填旧版本）旧的同意照旧能用关注、纠错，
# 但没告知过这些，所以同意工具使用说明、提交名单、开始加量体验时另外核对；
# 固定写在这里，回退配置也不放宽（同 AI 的 AI_AGREEMENT_VERSION）
TOOLS_AGREEMENT_VERSION = "2026-11"

# 版本号排在 TOOLS_AGREEMENT_VERSION 后面、正文却没有批量工具和加量体验条款的协议：
# "过审版加 AI"那一版（小程序 content/legal-first-release-ai.js，D-098）只写包里有的 AI，
# 去掉了这两项。
# 只同意过这些版本的人同意不了工具使用说明、提交不了名单、开始不了体验，要先同意含这两项的新版。
# 以后带批量工具的完整版协议，版本号要排在这些版本后面（比如 2026-12）
WITHOUT_TOOLS_TERMS = frozenset({"2026-11.1"})


def agreement_at_least(version: str | None, needed: str) -> bool:
    """同意的版本不早于 needed（如 AI 条款第一次写进去的那一版）。写法不对的不算。"""
    return bool(version) and VERSION_PREFIX.match(version) is not None and version >= needed


def tools_agreed(version: str | None) -> bool:
    """同意过写进批量工具和加量体验条款的版本：不早于 TOOLS_AGREEMENT_VERSION，
    也不是去掉了这两项的版本。"""
    return (
        agreement_at_least(version, TOOLS_AGREEMENT_VERSION) and version not in WITHOUT_TOOLS_TERMS
    )
