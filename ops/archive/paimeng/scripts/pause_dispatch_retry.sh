#!/usr/bin/env bash
# 13:52 的暂停脚本因 cron PATH 缺 ~/.local/bin 而失败（hermes: command not found），
# 网关没重启、配置没生效、派工还在继续。本脚本用绝对路径重做一次，立即生效。
set -u
export PATH=/home/liu/.local/bin:/usr/local/bin:/usr/bin:/bin
HERMES=/home/liu/.local/bin/hermes
LOG=/tmp/pause_dispatch_retry.log
echo "$(date '+%F %T') retry pause start" >> "$LOG"

crontab -l 2>/dev/null | grep -v "pause_dispatch_retry.sh" > /tmp/.ct4.$$ && crontab /tmp/.ct4.$$ 2>>"$LOG"
rm -f /tmp/.ct4.$$

running=$(python3 - <<'PY'
import sqlite3
con = sqlite3.connect("file:/home/liu/.hermes/kanban/boards/devteam/kanban.db?mode=ro", uri=True)
print(con.execute("select count(*) from tasks where status='running'").fetchone()[0])
PY
)
echo "$(date '+%F %T') running before = $running" >> "$LOG"

timeout 150 "$HERMES" gateway restart >> "$LOG" 2>&1
echo "$(date '+%F %T') restart rc=$?" >> "$LOG"
sleep 12
"$HERMES" gateway status >> "$LOG" 2>&1
grep -n "dispatch_interval_seconds" /home/liu/.hermes/config.yaml >> "$LOG"
echo "$(date '+%F %T') retry done" >> "$LOG"
