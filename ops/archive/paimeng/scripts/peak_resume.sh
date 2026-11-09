#!/usr/bin/env bash
# 12:03 恢复派工（午休是半价窗口）。做法同 peak_pause：改配置 + 重启网关。
set -u
export PATH=/home/liu/.local/bin:/usr/local/bin:/usr/bin:/bin
HERMES=/home/liu/.local/bin/hermes
LOG=/tmp/peak_resume.log
CFG=/home/liu/.hermes/config.yaml
echo "$(date '+%F %T') peak resume start" >> "$LOG"

/usr/bin/python3 - "$CFG" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p).read()
new = re.sub(r"^(\s*)dispatch_interval_seconds:.*$", r"\1dispatch_interval_seconds: 60", s, flags=re.M)
open(p, "w").write(new)
PY
grep -n dispatch_interval_seconds "$CFG" >> "$LOG"

crontab -l 2>/dev/null | grep -v "peak_resume.sh" > /tmp/.pr.$$ && crontab /tmp/.pr.$$ 2>>"$LOG"; rm -f /tmp/.pr.$$

timeout 150 "$HERMES" gateway restart >> "$LOG" 2>&1
echo "$(date '+%F %T') restart rc=$?" >> "$LOG"
sleep 10
"$HERMES" gateway status >> "$LOG" 2>&1
echo "$(date '+%F %T') peak resume done" >> "$LOG"
