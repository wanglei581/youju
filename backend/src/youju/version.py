"""发布包的来源版本；未知版本保持 unknown，不用本机配置伪造生产来源。"""

import json
import re
from pathlib import Path

BUILD_FILE = Path(__file__).with_name("_build.json")


def build_revision() -> str | None:
    try:
        value = json.loads(BUILD_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    revision = value.get("revision") if isinstance(value, dict) else None
    return (
        revision if isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision) else None
    )
