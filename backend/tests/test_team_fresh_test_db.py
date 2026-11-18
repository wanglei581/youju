"""tools/team/fresh-test-db.sh 取快照那一步（不连 MySQL）：D-127 以后快照从电脑导出、传到本机的目录里，
脚本取最新的一份、只留最近 3 份；没有就说清怎么拿。"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "team" / "fresh-test-db.sh"
needs_bash = pytest.mark.skipif(not shutil.which("bash"), reason="没有 bash")


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "YOUJU_PUBLIC_DIR": str(tmp_path / "public"), "TMPDIR": str(tmp_path)}
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=SCRIPT.parents[2],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


@needs_bash
def test_usage_says_where_the_snapshot_comes_from(tmp_path):
    done = _run(tmp_path, "-h")
    assert done.returncode == 2
    assert "快照从哪来" in done.stdout and "youju public-snapshot --output" in done.stdout
    assert "set -euo pipefail" not in done.stdout


@needs_bash
def test_needs_a_snapshot_from_the_pc(tmp_path):
    done = _run(tmp_path, "youju_tmp_snapshot_test")
    assert done.returncode == 1
    assert "里还没有公开快照" in done.stderr


@needs_bash
def test_takes_the_newest_snapshot_and_keeps_three(tmp_path):
    public = tmp_path / "public"
    public.mkdir()
    names = [f"youju-public-2026111{day}.jsonl.gz" for day in range(1, 6)]
    for name in names:
        (public / name).write_bytes(b"not a snapshot")
    (public / "notes.txt").write_text("x", encoding="utf-8")
    done = _run(tmp_path, "youju_tmp_snapshot_test")
    # 选到最新的一份；它不是真的快照，读说明行就停下（还没建库）
    assert "youju-public-20261115.jsonl.gz" in done.stdout
    assert done.returncode != 0
    assert sorted(p.name for p in public.iterdir()) == sorted([*names[-3:], "notes.txt"])
