#!/usr/bin/env bash
# 看板 Web 服务保活：8787 没在听就拉起来（网关重启会带走它，所以要 cron 守着）。
# 用绝对路径，避免再踩 PATH 的坑。
set -u
export PATH=/home/liu/.local/bin:/usr/local/bin:/usr/bin:/bin
LOG=/tmp/tb_serve_watchdog.log
REPO=/home/liu/project/team-board

if ss -ltn 2>/dev/null | grep -q ":8787 "; then
  exit 0
fi

echo "$(date '+%F %T') 8787 down → starting" >> "$LOG"
cd "$REPO" || exit 1
setsid "$REPO/.venv/bin/team-board" serve --port 8787 >> "$LOG" 2>&1 &
sleep 3
if curl -s -m 3 -o /dev/null -w "%{http_code}" http://127.0.0.1:8787/ | grep -q 200; then
  echo "$(date '+%F %T') started ok" >> "$LOG"
else
  echo "$(date '+%F %T') start FAILED" >> "$LOG"
fi
