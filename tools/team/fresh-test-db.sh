#!/bin/bash
# 用线上最新的公开内容建一个本机测试库（创始人 10/25："本机测试数据换成线上最新的公开内容，用户数据不出线上"）。
#
# 用法：tools/team/fresh-test-db.sh <库名> [--replace] [--file 快照文件] [--snapshot-code]
#   库名          youju_ 开头，只用小写字母、数字、下划线，例如 youju_pr301。不能是 youju（本机核对台的库），
#                 也不能 youju_test 开头（pytest 会把它整个删掉重建）。
#   --replace     库已存在时删掉重建（只动这一个库）。
#   --file        不下载，直接用这个快照文件（离线或复现时用）。
#   --snapshot-code  总是用快照说明行里的线上代码版本建表、导入（默认只在迁移版本不同时才这样做）。
#
# 做的事：
#   1. 下载线上最新的公开快照到 $YOUJU_PUBLIC_DIR（默认 ~/youju-worktrees/public-data，重启不丢，只留最近 3 份）。
#      用 tools/team/public-snapshot.cjs：只读，只认 public-snapshots/ 下的文件，整库备份一概不读。
#   2. 在本机 MySQL（$YOUJU_LOCAL_MYSQL，默认 mysql+pymysql://root@127.0.0.1:33306）新建空库，排序规则和线上一致。
#   3. 当前代码的最新迁移版本和快照相同，就用当前代码建表、导入；不同，就临时检出快照对应的线上代码
#      （说明行的 code_revision）来建表、导入，用完删掉。
#   4. 再用当前代码把库迁移到最新。当前代码比线上旧（没有线上的迁移版本）就在建库前停下，提示先合最新 origin/main。
# 最后一行输出库的连接地址。测试窗口名写上脚本打印的"数据MM/DD"，给创始人看时说明数据日期（AGENTS.md 第 9 条）。
set -euo pipefail

usage() {
  sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

NAME=""
REPLACE=0
FILE=""
FORCE_CODE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --replace) REPLACE=1 ;;
    --file)
      shift
      FILE="${1:-}"
      ;;
    --snapshot-code) FORCE_CODE=1 ;;
    -h | --help) usage ;;
    -*)
      echo "不认识的参数：$1" >&2
      usage
      ;;
    *)
      [ -z "$NAME" ] || usage
      NAME="$1"
      ;;
  esac
  shift
done
[ -n "$NAME" ] || usage
if ! printf '%s' "$NAME" | grep -Eq '^youju_[a-z0-9_]{1,48}$'; then
  echo "库名要 youju_ 开头，只用小写字母、数字、下划线：$NAME" >&2
  exit 2
fi
case "$NAME" in
  youju_test | youju_test_*)
    echo "不能用 youju_test 开头的库名：pytest 会把它整个删掉重建" >&2
    exit 2
    ;;
esac

REPO=$(git rev-parse --show-toplevel)
BACKEND="$REPO/backend"
[ -d "$BACKEND/src/youju" ] || {
  echo "要在有据仓库（或它的 worktree）里运行" >&2
  exit 2
}
SERVER=${YOUJU_LOCAL_MYSQL:-mysql+pymysql://root@127.0.0.1:33306}
URL="$SERVER/$NAME?charset=utf8mb4"
DIR=${YOUJU_PUBLIC_DIR:-$HOME/youju-worktrees/public-data}
TMP=""
LOG=$(mktemp "${TMPDIR:-/tmp}/youju-fresh-db.XXXXXX")
cleanup() {
  rm -f "$LOG"
  if [ -n "$TMP" ]; then
    git -C "$REPO" worktree remove --force "$TMP/code" > /dev/null 2>&1 || true
    rm -rf "$TMP"
  fi
}
trap cleanup EXIT

# 准备某份代码的运行环境（先用本机缓存，不行再联网装依赖），然后在它的 backend 里跑 python
ensure_env() { (cd "$1" && { UV_OFFLINE=1 uv sync --quiet --frozen 2> /dev/null || uv sync --quiet --frozen; }); }
py() {
  local dir="$1"
  shift
  (cd "$dir" && YOUJU_DATABASE_URL="$URL" uv run --no-sync --quiet python "$@")
}

# 1. 快照文件
if [ -z "$FILE" ]; then
  FILE=$(node "$REPO/tools/team/public-snapshot.cjs" "$DIR" | tail -1)
fi
[ -s "$FILE" ] || {
  echo "快照文件不存在或是空的：$FILE" >&2
  exit 1
}
META=$(python3 - "$FILE" << 'PY'
import gzip, json, sys
from datetime import datetime, timedelta, timezone
with gzip.open(sys.argv[1], "rb") as fh:
    header = json.loads(fh.readline())
if not isinstance(header, dict) or header.get("format") != "youju-public-snapshot":
    sys.exit("不是有据的公开快照文件")
at = datetime.fromisoformat(header["created_at"]).astimezone(timezone(timedelta(hours=8)))
print(header.get("revision") or "-", header.get("code_revision") or "-", at.strftime("%m/%d_%H:%M"), at.strftime("%m/%d"))
PY
)
read -r REV CODE STAMP DAY <<< "$META"

# 2. 当前代码认不认识快照的迁移版本：不认识就是代码比线上旧，先停下
ensure_env "$BACKEND"
HEADS=$(py "$BACKEND" - "$REV" << 'PY'
import os, sys
from alembic.script import ScriptDirectory
from youju.migrations import alembic_config
script = ScriptDirectory.from_config(alembic_config(os.environ["YOUJU_DATABASE_URL"]))
try:
    script.get_revision(sys.argv[1])
except Exception:
    sys.exit(f"当前代码没有线上的迁移版本 {sys.argv[1]}：先把最新 origin/main 合进来再建库")
print(" ".join(sorted(script.get_heads())))
PY
)

# 3. 新建空库
py "$BACKEND" - "$SERVER" "$NAME" "$REPLACE" << 'PY'
import sys
from sqlalchemy import create_engine, text
server, name, replace = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
engine = create_engine(server + "/?charset=utf8mb4")
with engine.begin() as conn:
    found = conn.execute(
        text("SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = :n"), {"n": name}
    ).scalar()
    if found and not replace:
        sys.exit(f"库 {name} 已经有了：要删掉重建就加 --replace")
    conn.execute(text(f"DROP DATABASE IF EXISTS `{name}`"))
    conn.execute(text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"))
engine.dispose()
PY

# 4. 选建表、导入用的代码：迁移版本相同就用当前代码，否则临时检出线上那一版
CODE_DIR="$BACKEND"
if [ "$FORCE_CODE" = 1 ] || [ "$HEADS" != "$REV" ]; then
  if [ "$CODE" = "-" ]; then
    echo "快照没写线上代码版本，当前代码的迁移版本（$HEADS）又和快照（$REV）不同，没法导入" >&2
    exit 1
  fi
  git -C "$REPO" cat-file -e "$CODE^{commit}" 2> /dev/null || git -C "$REPO" fetch -q origin
  TMP=$(mktemp -d "${TMPDIR:-/tmp}/youju-snapshot-code.XXXXXX")
  git -C "$REPO" worktree add -q --detach "$TMP/code" "$CODE"
  CODE_DIR="$TMP/code/backend"
  echo "用线上代码 ${CODE:0:8} 建表、导入（当前代码的迁移版本是 $HEADS，快照是 $REV）" >&2
  ensure_env "$CODE_DIR"
fi

# 建表（迁移日志写进 $LOG，出错才显示）；迁移会预置少量行（如周报"全国版"方案），这是刚建的新库，
# 导入前把公开内容表清空，导入时才查得出"不是空的"这类真问题
if ! py "$CODE_DIR" - "$REV" > "$LOG" 2>&1 << 'PY'; then
import os, sys
from sqlalchemy import text
from youju.db import create_mysql_engine
from youju.migrations import upgrade
from youju.public_snapshot import public_tables
url = os.environ["YOUJU_DATABASE_URL"]
if upgrade(url) is not None:
    sys.exit("新库的迁移版本不对，没法导入")
engine = create_mysql_engine(url)
with engine.begin() as conn:
    got = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    if got != sys.argv[1]:
        sys.exit(f"建表后的迁移版本 {got} 和快照 {sys.argv[1]} 不同，没法导入")
    conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
    try:
        for table in public_tables():
            conn.execute(text(f"DELETE FROM `{table.name}`"))
    finally:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
engine.dispose()
PY
  cat "$LOG" >&2
  exit 1
fi
(cd "$CODE_DIR" && YOUJU_DATABASE_URL="$URL" uv run --no-sync --quiet youju restore-public "$FILE") >&2

# 5. 用当前代码迁移到最新
if ! py "$BACKEND" - > "$LOG" 2>&1 << 'PY'; then
import os, sys
from youju.migrations import upgrade
if upgrade(os.environ["YOUJU_DATABASE_URL"]) is not None:
    sys.exit("库比当前代码新：先把最新 origin/main 合进来")
PY
  cat "$LOG" >&2
  exit 1
fi

echo "本机测试库 $NAME 已建好：线上公开内容 ${STAMP/_/ }（北京时间），不含用户数据。" >&2
echo "测试窗口名请带上\"数据$DAY\"，例如\"有据-测试（Claude·#PR号 提交号·数据$DAY）\"。" >&2
echo "$URL"
