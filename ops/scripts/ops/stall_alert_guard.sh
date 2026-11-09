#!/usr/bin/env bash
# 卡死告警的 guard 包装：写心跳 .ok + 把告警文本交给 hermes cron 投递。
# （不能直接用 guard_run.sh：它吞掉 stdout，告警就发不出去）
set -u
export PATH=/home/liu/.local/bin:/usr/local/bin:/usr/bin:/bin
GDIR=/home/liu/.hermes/profiles/ops/workspace/guard
mkdir -p "$GDIR"
out=$(/usr/bin/python3 /home/liu/.hermes/profiles/ops/scripts/stall_alert.py 2>&1)
rc=$?
lines=$(printf '%s\n' "$out" | wc -l)
printf 'ts=%s rc=%s lines=%s period=10\n' "$(date +%s)" "$rc" "$lines" > "$GDIR/stall_alert.ok"
{ echo "== $(date '+%F %T') rc=$rc lines=$lines"; printf '%s\n' "$out"; } >> "$GDIR/stall_alert.log"
[ -n "$out" ] && printf '%s\n' "$out"
exit 0
