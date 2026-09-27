import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent / "js" / "review_highlight.cjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_review_console_date_highlight_cases() -> None:
    """核对台只在年月日都对得上时标黄（省略年份、跨年、次年等写法）。"""
    result = subprocess.run(
        ["node", str(SCRIPT)], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
