#!/bin/sh
# 第 1 批上线（D-126）：国家级 + 省级全部门。步骤和核对见 docs/数据来源/全国覆盖/批次/第1批_上线步骤.md。
# 只在前台跑，不要 nohup、不要放后台：
#   /app/backend/deploy/batch1.sh --check   先试算（不改库）
#   /app/backend/deploy/batch1.sh --apply   创始人点头后再跑；每一步先试算、核对，对不上就停
# 每一步打一行纯 ASCII 的结论（step=… ok=1），同时写进 /tmp/batch1-<时间>.txt；
# 最后一行 batch1_done=1 enabled=<个数>。对不上的网址写进同名的 -detail.txt。
set -eu
case "${1:-}" in
  --check|--apply) ;;
  *) echo "usage: batch1.sh --check|--apply"; exit 2 ;;
esac
exec youju coverage-golive --batch "第1批_国家级和省级" --level 国家级 --level 省级 \
  --by "Claude（主线）" --operator "协调会话" --tag batch1 "$1"
