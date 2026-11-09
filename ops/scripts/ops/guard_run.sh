#!/usr/bin/env bash
# 看护件统一包装器：跑一条命令，记录 exit code 与输出行数，写心跳 .ok。
# 用法：guard_run.sh <件名> <周期分钟> <完整命令...>
# 产物：workspace/guard/<件名>.ok（ts/rc/lines）、workspace/guard/<件名>.log（累积输出）
# 设计：本脚本永远 exit 0（不让 cron 发失败邮件）；失败信息留在 .ok 里由 guard_health.py 判。
set -u

export PATH=/home/liu/.local/bin:/usr/local/bin:/usr/bin:/bin
GDIR=/home/liu/.hermes/profiles/ops/workspace/guard
mkdir -p "$GDIR"

name=${1:?需要件名}; shift
period=${1:?需要周期分钟}; shift

out=$("$@" 2>&1)
rc=$?
lines=$(printf '%s\n' "$out" | wc -l)
ts=$(date +%s)

{
  echo "== $(date '+%F %T') rc=$rc lines=$lines period=${period}m"
  printf '%s\n' "$out"
} >> "$GDIR/$name.log"

# 日志别无限长：超过 2MB 截半
if [ -f "$GDIR/$name.log" ] && [ "$(stat -c %s "$GDIR/$name.log")" -gt 2097152 ]; then
  tail -c 1000000 "$GDIR/$name.log" > "$GDIR/$name.log.tmp" && mv "$GDIR/$name.log.tmp" "$GDIR/$name.log"
fi

printf 'ts=%s rc=%s lines=%s period=%s\n' "$ts" "$rc" "$lines" "$period" > "$GDIR/$name.ok"
exit 0
