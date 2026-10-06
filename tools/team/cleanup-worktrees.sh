#!/bin/sh
# 清理用完的 git worktree，释放磁盘空间（创始人 10/6 要求：更新后没用的文件要及时清掉）。
# 用法：cleanup-worktrees.sh [--dry-run] [要保留的目录名 ...]
# 只删同时满足这两条的：① 没有未提交的改动、没有未跟踪的文件（被 .gitignore 忽略的如 .venv、.env 会一起删掉）；
# ② 当前提交已经在 GitHub 上某个分支里（代码都推上去了，删目录不丢东西）。分支本身不删。
# 始终保留：ai（主线）、ai-internal（AI 内测包）、miniapp-release（创始人的上传目录）、mysql（本机数据库）、closeout（文档工作区）。
ROOT=/Users/wanglei/youju-worktrees
MAIN=$ROOT/ai
DRY=0
[ "$1" = "--dry-run" ] && { DRY=1; shift; }
KEEP="ai ai-internal miniapp-release mysql closeout $*"
git -C "$MAIN" fetch -q --prune origin
git -C "$MAIN" worktree prune
for dir in "$ROOT"/*/; do
  name=$(basename "$dir")
  case " $KEEP " in *" $name "*) continue;; esac
  git -C "$dir" rev-parse --git-dir >/dev/null 2>&1 || { echo "跳过 $name：不是 git 工作区"; continue; }
  if [ -n "$(git -C "$dir" status --porcelain 2>/dev/null)" ]; then
    echo "保留 $name：有未提交的改动或未跟踪的文件"; continue
  fi
  head=$(git -C "$dir" rev-parse HEAD)
  if [ -z "$(git -C "$MAIN" branch -r --contains "$head" 2>/dev/null)" ]; then
    echo "保留 $name：有提交还没推到 GitHub"; continue
  fi
  size=$(du -sh "$dir" 2>/dev/null | cut -f1)
  if [ $DRY = 1 ]; then echo "可删 $name（$size）"; continue; fi
  if git -C "$MAIN" worktree remove --force "$dir" 2>/dev/null; then echo "已删 $name（$size）"; else echo "删除失败 $name"; fi
done
git -C "$MAIN" worktree prune
du -sh "$ROOT" 2>/dev/null
