"""小程序里的协议版本号要过得了后台的几道协议门槛（D-098、D-117）。

完整版协议（小程序 content/legal.js，11/4 起是 2026-12 的草稿）带批量工具、加量体验和付费条款，也写了
AI 结果送微信内容安全检查：版本号要排在"不含工具条款"的 2026-11.1 后面（不在 WITHOUT_TOOLS_TERMS 里），
算同意过批量工具和加量体验条款（tools_agreed），也满足 AI 条款的门槛（AI_AGREEMENT_VERSION）。
过审版加 AI（2026-11.1）、过审版（2026-10.2）照旧各守各的门槛。直接读小程序的协议文件：
以后协议升版或改了后台常量、两边对不上时，这里先失败。不连数据库。
"""

import re
from pathlib import Path

from youju.agreements import (
    TOOLS_AGREEMENT_VERSION,
    VERSION_FULL,
    WITHOUT_TOOLS_TERMS,
    agreement_at_least,
    tools_agreed,
)
from youju.ai.interpret import AI_AGREEMENT_VERSION

CONTENT = Path(__file__).resolve().parents[2] / "apps" / "miniapp" / "content"


def _version(name: str) -> str:
    text = (CONTENT / name).read_text(encoding="utf-8")
    match = re.search(r'^\s*"?version"?\s*:\s*["\']([^"\']+)["\']', text, re.M)
    assert match, f"读不到 {name} 的版本号"
    return match.group(1)


def test_full_agreement_passes_tools_and_ai_gates():
    version = _version("legal.js")
    assert VERSION_FULL.fullmatch(version)
    # D-117 起完整版至少是 2026-12：排在过审版加 AI 2026-11.1 后面
    assert version >= "2026-12"
    assert version not in WITHOUT_TOOLS_TERMS
    assert all(version > other for other in WITHOUT_TOOLS_TERMS)
    assert agreement_at_least(version, TOOLS_AGREEMENT_VERSION)
    assert tools_agreed(version)
    assert agreement_at_least(version, AI_AGREEMENT_VERSION)


def test_first_release_agreements_keep_their_own_gates():
    ai_version = _version("legal-first-release-ai.js")
    # 过审版加 AI：含 AI 条款，不含批量工具和加量体验
    assert ai_version in WITHOUT_TOOLS_TERMS
    assert not tools_agreed(ai_version)
    assert agreement_at_least(ai_version, AI_AGREEMENT_VERSION)
    # 过审版：两样都不含
    first = _version("legal-first-release.js")
    assert not tools_agreed(first)
    assert not agreement_at_least(first, AI_AGREEMENT_VERSION)
    # 完整版排在两者后面：只同意过它们的人，在完整版里会被请去重新同意
    assert _version("legal.js") > ai_version > first
