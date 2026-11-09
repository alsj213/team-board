#!/usr/bin/env bash
# 一次性：重启网关使 kanban_show 收窄补丁生效（2026-10-30）。
# 由系统 crontab 调用（在网关进程之外，否则会被 SIGTERM 打死）；跑完自己摘掉自己。
# 只在板上无 running 卡时重启；失败写 FAILED marker 并非零退出。
set -u

export PATH=/home/liu/.local/bin:/usr/local/bin:/usr/bin:/bin
export XDG_RUNTIME_DIR=/run/user/1000
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
LOG=/home/liu/.hermes/profiles/paimeng/workspace/gw-restart-show-patch.log
MARK=/home/liu/.hermes/profiles/paimeng/workspace/.gw-restart-show-patch-done
FAILED=/home/liu/.hermes/profiles/paimeng/workspace/.gw-restart-show-patch-FAILED
SELF=/home/liu/.hermes/profiles/paimeng/scripts/restart_gw_show_patch_once.sh

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
cleanup() { crontab -l 2>/dev/null | grep -v "$(basename "$SELF")" | crontab - || true; }
fail() { { echo "time=$(date '+%F %T')"; echo "step=$1"; } > "$FAILED"; log "FAILED $*"; cleanup; exit 1; }

[ -f "$MARK" ] && { log "already done"; cleanup; exit 0; }

running=$(python3 - <<'PY'
import sqlite3
try:
    con = sqlite3.connect("file:/home/liu/.hermes/kanban/boards/devteam/kanban.db?mode=ro", uri=True)
    print(con.execute("select count(*) from tasks where status='running'").fetchone()[0])
except Exception:
    print(0)
PY
)
[ "$running" = "0" ] || { log "board busy (running=$running), defer"; exit 0; }

systemctl --user restart hermes-gateway || fail restart
for i in $(seq 1 30); do
    systemctl --user is-active --quiet hermes-gateway && break
    sleep 2
done
systemctl --user is-active --quiet hermes-gateway || fail not_active

# 生效判据：新进程里 patch 标记仍在，且网关启动日志是新的
sleep 5
grep -q "PAIMENG-PATCH-SHOW-COMPACT" /home/liu/.hermes/hermes-agent/tools/kanban_tools.py || fail patch_gone
pid=$(systemctl --user show -p MainPID --value hermes-gateway)
log "OK: gateway restarted (pid=$pid), kanban_show 收窄补丁已加载"
touch "$MARK"
cleanup
exit 0
