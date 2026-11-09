#!/usr/bin/env bash
# 一次性：重启网关让 dispatch_interval_seconds=86400 生效（停止派工）。
# 由系统 crontab 调用（在网关进程之外，否则会被 SIGTERM 打死）；跑完自己摘掉自己。
set -u
export PATH=/home/liu/.local/bin:/usr/local/bin:/usr/bin:/bin
export XDG_RUNTIME_DIR=/run/user/1000
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
LOG=/home/liu/.hermes/profiles/ops/workspace/pause-dispatch.log
MARK=/home/liu/.hermes/profiles/ops/workspace/.pause-dispatch-done
SELF=/home/liu/.hermes/profiles/ops/scripts/pause_dispatch_once.sh

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
cleanup() { crontab -l 2>/dev/null | grep -v "$(basename "$SELF")" | crontab - || true; }

[ -f "$MARK" ] && { cleanup; exit 0; }

systemctl --user restart hermes-gateway || { log "FAILED restart"; cleanup; exit 1; }
for i in $(seq 1 30); do
    systemctl --user is-active --quiet hermes-gateway && break
    sleep 2
done
systemctl --user is-active --quiet hermes-gateway || { log "FAILED not active"; cleanup; exit 1; }

sleep 8
running=$(python3 - <<'PY'
import sqlite3
try:
    con = sqlite3.connect("file:/home/liu/.hermes/kanban/boards/devteam/kanban.db?mode=ro", uri=True)
    print(con.execute("select count(*) from tasks where status='running'").fetchone()[0])
except Exception:
    print(-1)
PY
)
log "OK: gateway restarted, running=$running, dispatch=86400"
touch "$MARK"
cleanup
exit 0
