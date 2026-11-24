"""API 和后台任务共用的协议版本判断，不依赖 API 模块。"""

import re

VERSION_PREFIX = re.compile(r"\d{4}-\d{2}")
# 协议版本的完整写法：年-月，可带一段 .数字（如 2026-10.2、2026-11.1）
VERSION_FULL = re.compile(r"\d{4}-\d{2}(\.\d+)?")

# 批量工具的任务记录和次数（隐私保护指引一·13）、加量体验（一·15）写进指引的那一版。
# 协议升版过渡期（YOUJU_AGREEMENT_MIN_VERSION 填旧版本）旧的同意照旧能用关注、纠错，
# 但没告知过这些，所以同意工具使用说明、提交名单、开始加量体验时另外核对；
# 固定写在这里，回退配置也不放宽（同 AI 的 AI_AGREEMENT_VERSION）
TOOLS_AGREEMENT_VERSION = "2026-11"

# 版本号排在 TOOLS_AGREEMENT_VERSION 后面、正文却没有批量工具和加量体验条款的协议：
# "过审版加 AI"那一版（小程序 content/legal-first-release-ai.js，D-098）只写包里有的 AI，
# 去掉了这两项。
# 只同意过这些版本的人同意不了工具使用说明、提交不了名单、开始不了体验，要先同意含这两项的新版。
# 带批量工具的完整版协议排在这些版本后面：2026-12（11/4 起草，小程序 content/legal.js，D-117），
# 不放进这里（tests/test_agreement_versions.py 核对小程序里的版本号）
WITHOUT_TOOLS_TERMS = frozenset({"2026-11.1"})

# "问一问使用说明"（《隐私保护指引》的正式补充，问一问开发设计 #328 第十三节第 1 条）的版本：
# 白名单和对所有人开放时都要单独同意，发一问时核对（api/chat.py），admin 处理问答任务时再核一次。
# 文字定稿以后不改；改字就升版本、请大家重新同意（小程序 content/chat-terms.js 的 version 一致）
CHAT_AGREEMENT_VERSION = "chat-2026-12-v1"

# "您保存的通知"（D-123 ⑤，我的保存）写进隐私保护指引的那一版：保存时核对，没同意过的先请他
# 重新同意（同 TOOLS_AGREEMENT_VERSION，回退配置也不放宽）。2.0 的新版指引还没定稿，先写草稿
# 建议的版本号（docs/合规/隐私保护指引_2.0自建服务器_草稿.md 第二节）；定稿时改成真正的版本号，
# 比现行 2026-12 新：在那之前就算打开开关，也没人能保存
SAVED_AGREEMENT_VERSION = "2027-01"


def agreement_at_least(version: str | None, needed: str) -> bool:
    """同意的版本不早于 needed（如 AI 条款第一次写进去的那一版）。写法不对的不算。"""
    return bool(version) and VERSION_PREFIX.match(version) is not None and version >= needed


def tools_agreed(version: str | None) -> bool:
    """同意过写进批量工具和加量体验条款的版本：不早于 TOOLS_AGREEMENT_VERSION，
    也不是去掉了这两项的版本。"""
    # 只认完整写法：2026-11.1 后面多了字符（如"2026-11.1x"）的不算，免得绕过下面的排除
    return (
        agreement_at_least(version, TOOLS_AGREEMENT_VERSION)
        and VERSION_FULL.fullmatch(version) is not None
        and version not in WITHOUT_TOOLS_TERMS
    )
