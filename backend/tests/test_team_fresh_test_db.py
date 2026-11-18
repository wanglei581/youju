"""tools/team/fresh-test-db.sh 取快照那一步（不连 MySQL）：D-127 以后快照从电脑导出、传到本机的目录里，
脚本取最新的一份，那里只留 7 天以内、最近的 3 份；没有就说清怎么拿。"""

import gzip
import os
import shutil
import subprocess
from datetime import date, timedelta
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


def _name(days_ago: int) -> str:
    return f"youju-public-{date.today() - timedelta(days=days_ago):%Y%m%d}.jsonl.gz"


@needs_bash
def test_usage_says_where_the_snapshot_comes_from(tmp_path):
    done = _run(tmp_path, "-h")
    assert done.returncode == 2
    assert "快照从哪来" in done.stdout and "youju public-snapshot --output" in done.stdout
    assert "紧急下架" in done.stdout and "_build.json" in done.stdout
    assert "set -euo pipefail" not in done.stdout


@needs_bash
def test_needs_a_recent_snapshot_from_the_pc(tmp_path):
    done = _run(tmp_path, "youju_tmp_snapshot_test")
    assert done.returncode == 1
    assert "里没有 7 天以内的公开快照" in done.stderr
    # 只有 7 天以前的：删掉，照样说没有
    public = tmp_path / "public"
    (public / _name(8)).write_bytes(b"x")
    done = _run(tmp_path, "youju_tmp_snapshot_test")
    assert done.returncode == 1 and "里没有 7 天以内的公开快照" in done.stderr
    assert not list(public.iterdir())


@needs_bash
def test_takes_the_newest_snapshot_and_keeps_three_recent_ones(tmp_path):
    public = tmp_path / "public"
    public.mkdir()
    # 说明行不对的快照：选中以后读说明行就停下（还没建库）
    blob = gzip.compress(b'{"format": "something-else"}\n')
    recent = [_name(days) for days in range(4, -1, -1)]  # 4 天前到今天
    for name in [*recent, _name(10)]:
        (public / name).write_bytes(blob)
    (public / "notes.txt").write_text("x", encoding="utf-8")
    done = _run(tmp_path, "youju_tmp_snapshot_test")
    assert done.returncode != 0
    assert f"最新的一份：{_name(0)}" in done.stderr
    assert "不是有据的公开快照文件" in done.stderr
    # 7 天以前的删掉，7 天以内的只留最近 3 份，别的文件不动；标准输出不打选了哪份（只留连接地址）
    assert sorted(p.name for p in public.iterdir()) == sorted([*recent[-3:], "notes.txt"])
    assert "最新的一份" not in done.stdout
