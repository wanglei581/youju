#!/bin/bash
# 用电脑（公开数据的主库）导出的公开快照，建一个本机测试库（创始人 10/25："本机测试数据换成线上最新的公开内容，
# 用户数据不出线上"；D-127 以后云托管停用，快照改从电脑出）。
#
# 用法：tools/team/fresh-test-db.sh <库名> [--replace] [--file 快照文件] [--snapshot-code]
#   库名          youju_ 开头，只用小写字母、数字、下划线，例如 youju_pr301。不能是 youju（本机核对台的库），
#                 也不能 youju_test 开头（pytest 会把它整个删掉重建）。
#   --replace     库已存在时先删掉再重建（只动这一个库；中途失败的话原来的库也没了）。
#   --file        用这个快照文件（不给就用 $YOUJU_PUBLIC_DIR 里最新的一份）。
#   --snapshot-code  总是用快照说明行里的代码版本建表、导入（默认只在迁移版本不同时才这样做）。
#
# 快照从哪来：在电脑上导出，要进电脑（创始人同意以后，在 UU 远程的终端里跑这一行）：
#   wsl -d youju -u root --cd /opt/youju/local -e bash -c 'mkdir -p /mnt/e/youju/public-snapshots && docker compose run --rm --no-deps -v /mnt/e/youju/public-snapshots:/out worker youju public-snapshot --output /out/youju-public-$(date +%Y%m%d).jsonl.gz'
#   再用 UU 的文件传输，把 E:\youju\public-snapshots 里那份传到这台 Mac 的 $YOUJU_PUBLIC_DIR
#   （默认 ~/youju-worktrees/public-data），当天就删掉电脑上那份（传失败也删：
#   wsl -d youju -u root -e rm -f /mnt/e/youju/public-snapshots/<文件名>）。
#   快照里只有公开内容表，不含用户数据、密钥、日志和下架的文件（backend/src/youju/public_snapshot.py）。
#   导出前看一眼服务器上有没有还没带回电脑的紧急下架（手册第八节"紧急下架"第 3 步 emergency-list；要进服务器，
#   创始人扫码登录服务器控制台、进网页终端）：有的话先照第 5 步带回电脑，不然那些文件还在快照里。
#   电脑要装过含 #530 的提交打的安装包（带 _build.json），快照里才记得下代码版本；没记的话，本机代码的迁移版本
#   和快照不同时建不了库。
#
# 做的事：
#   1. 取 $YOUJU_PUBLIC_DIR 里最新的一份（youju-public-<8 位日期>.jsonl.gz），那里只留 7 天以内、最近的 3 份。
#   2. 在本机 MySQL（$YOUJU_LOCAL_MYSQL，默认 mysql+pymysql://root@127.0.0.1:33306）新建空库，排序规则和服务器一致。
#   3. 当前代码的最新迁移版本和快照相同，就用当前代码建表、导入；不同，就临时检出快照对应的代码
#      （说明行的 code_revision）来建表、导入，用完删掉。
#   4. 再用当前代码把库迁移到最新。当前代码比快照旧（没有快照的迁移版本）就在建库前停下，提示先合最新 origin/main。
#   中途失败会删掉建了一半的库，修好原因后直接重跑。
# 前提：$YOUJU_PUBLIC_DIR 里有快照（或用 --file 指定）；本机 MySQL 在跑（没在跑就在 youju-worktrees/mysql 里启动）。
# 最后一行输出库的连接地址：把本机后台的 YOUJU_DATABASE_URL 设成它再启动后台。测试窗口名写上脚本打印的
# "数据MM/DD"，给创始人看时说明数据日期（AGENTS.md 第 9 条）。
set -euo pipefail

usage() {
  # 开头的说明（第 2 行到 set -euo pipefail 之前）
  sed -n '2,/^set -euo pipefail/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
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
CREATED=0
LOG=$(mktemp "${TMPDIR:-/tmp}/youju-fresh-db.XXXXXX")
cleanup() {
  local code=$?
  rm -f "$LOG"
  if [ "$code" != 0 ] && [ "$CREATED" = 1 ]; then
    # 建了一半的库删掉，下次直接重跑（只删这个脚本刚建的库）
    py "$BACKEND" - "$SERVER" "$NAME" << 'PY' > /dev/null 2>&1 || true
import sys
from sqlalchemy import create_engine, text
engine = create_engine(sys.argv[1] + "/?charset=utf8mb4")
with engine.begin() as conn:
    conn.execute(text(f"DROP DATABASE IF EXISTS `{sys.argv[2]}`"))
engine.dispose()
PY
    echo "没建成，已删掉建了一半的库 $NAME" >&2
  fi
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

# 1. 快照文件：没给 --file 就用 $DIR 里最新的一份（文件名里是导出那天的日期）。那里只留 7 天以内、最近的 3 份，
#    别的删掉（以前云托管上的快照也只留 7 天）；一份都没有就说清怎么拿
if [ -z "$FILE" ]; then
  mkdir -p "$DIR"
  FILE=$(python3 -I - "$DIR" << 'PY'
import datetime, pathlib, re, sys
folder = pathlib.Path(sys.argv[1])
today = datetime.date.today()
names = sorted(p.name for p in folder.iterdir() if re.fullmatch(r"youju-public-\d{8}\.jsonl\.gz", p.name))
def fresh(name):
    try:
        day = datetime.datetime.strptime(name[13:21], "%Y%m%d").date()
    except ValueError:
        return False
    # 7 天以内的留着；两边时钟、时区不一，日期最多容许比今天晚 1 天，再晚的（时钟错、改过名）也删
    return -1 <= (today - day).days <= 7
keep = [name for name in names if fresh(name)][-3:]
for name in names:
    if name not in keep:
        (folder / name).unlink()
print(folder / keep[-1] if keep else "")
PY
)
  [ -n "$FILE" ] || {
    echo "$DIR 里没有 7 天以内的公开快照：先在电脑上导出、传过来（看本脚本开头的说明，或 -h），或者用 --file 指定" >&2
    exit 1
  }
  echo "用 $DIR 里最新的一份：$(basename "$FILE")" >&2
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

# 2. 当前代码认不认识快照的迁移版本：不认识就是代码比快照旧，先停下
ensure_env "$BACKEND"
HEADS=$(py "$BACKEND" - "$REV" << 'PY'
import os, sys
from alembic.script import ScriptDirectory
from youju.migrations import alembic_config
script = ScriptDirectory.from_config(alembic_config(os.environ["YOUJU_DATABASE_URL"]))
try:
    script.get_revision(sys.argv[1])
except Exception:
    sys.exit(f"当前代码没有快照的迁移版本 {sys.argv[1]}：先把最新 origin/main 合进来再建库")
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
CREATED=1

# 4. 选建表、导入用的代码：迁移版本相同就用当前代码，否则临时检出快照对应的那一版
CODE_DIR="$BACKEND"
if [ "$FORCE_CODE" = 1 ] || [ "$HEADS" != "$REV" ]; then
  if [ "$CODE" = "-" ]; then
    if [ "$FORCE_CODE" = 1 ]; then
      echo "快照没写代码版本，没法按 --snapshot-code 用快照对应的代码导入；去掉这个参数再试" >&2
    else
      echo "快照没写代码版本，当前代码的迁移版本（$HEADS）又和快照（$REV）不同，没法导入" >&2
    fi
    exit 1
  fi
  git -C "$REPO" cat-file -e "$CODE^{commit}" 2> /dev/null || git -C "$REPO" fetch -q origin
  TMP=$(mktemp -d "${TMPDIR:-/tmp}/youju-snapshot-code.XXXXXX")
  git -C "$REPO" worktree add -q --detach "$TMP/code" "$CODE"
  CODE_DIR="$TMP/code/backend"
  echo "用快照对应的代码 ${CODE:0:8} 建表、导入（当前代码的迁移版本是 $HEADS，快照是 $REV）" >&2
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

echo "本机测试库 $NAME 已建好：公开内容快照 ${STAMP/_/ }（北京时间导出），不含用户数据。" >&2
echo "测试窗口名请带上\"数据$DAY\"，例如\"有据-测试（Claude·#PR号 提交号·数据$DAY）\"。" >&2
CREATED=0
echo "$URL"
