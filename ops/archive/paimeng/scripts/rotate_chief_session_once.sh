#!/usr/bin/env bash
# 一次性：轮换 chief 的飞书长命会话（2026-10-30）。
# 背景：chief 的 feishu dm 会话从 10-23 02:27 起没断，245 条消息、单次缓存读 13.8 万 tok。
# 机制：SessionStore 只在网关启动时加载路由（gateway_routing 主、sessions.json 旧版导入），
#       运行中改文件不会被读；而 entry.suspended=true 会在下次访问时强制 auto-reset 成新会话。
#       所以 = 先写 suspended（网关里存的内存副本会被覆盖，必须在重启前一刻写）+ 重启网关。
# 由系统 crontab 调用（在网关进程之外，否则会被 SIGTERM 打死）；跑完自己摘掉自己。
# 失败绝不静默：写 FAILED marker 并非零退出。
set -u

export PATH=/home/liu/.local/bin:/usr/local/bin:/usr/bin:/bin
# cron 下必须显式给 user bus 的地址，否则 `systemctl --user` 报 Failed to connect to bus
export XDG_RUNTIME_DIR=/run/user/1000
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
HERMES_HOME_DIR=/home/liu/.hermes
SESS=$HERMES_HOME_DIR/sessions/sessions.json
CHIEF_DB=$HERMES_HOME_DIR/profiles/chief/state.db
LOG=$HERMES_HOME_DIR/profiles/chief/workspace/rotate-session.log
MARK=$HERMES_HOME_DIR/profiles/chief/workspace/.rotate-done
FAILED=$HERMES_HOME_DIR/profiles/chief/workspace/.rotate-FAILED
KEY="agent:chief:feishu:dm:oc_bbe0682094e2acd3be2e35c9caca0f9c"
SELF=/home/liu/.hermes/profiles/paimeng/scripts/rotate_chief_session_once.sh

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# 停过网关就必须保证再起来：任何失败路径（fail/exit/异常）都走这里兜底
STOPPED=0
on_exit() {
    if [ "$STOPPED" = "1" ] && ! systemctl --user is-active --quiet hermes-gateway; then
        systemctl --user start hermes-gateway || true
        log "safety net: gateway restarted on exit"
    fi
}
trap on_exit EXIT

fail() {
    { echo "time=$(date '+%F %T')"; echo "step=$1"; echo "rc=${2:-1}"; shift 2 || true; echo "$*"; } > "$FAILED"
    log "FAILED step=$1 rc=${2:-1} $*"
    crontab -l 2>/dev/null | grep -v "$(basename "$SELF")" | crontab - || true
    exit 1
}

[ -f "$MARK" ] && { log "already done, exit"; crontab -l 2>/dev/null | grep -v "$(basename "$SELF")" | crontab - || true; exit 0; }

# 0. 看板必须空闲，避免打断在跑的卡
running=$(python3 - <<'PY'
import sqlite3
try:
    con=sqlite3.connect("file:/home/liu/.hermes/kanban/boards/devteam/kanban.db?mode=ro",uri=True)
    print(con.execute("select count(*) from tasks where status='running'").fetchone()[0])
except Exception:
    print(0)
PY
)
[ "$running" = "0" ] || { log "board busy (running=$running), defer"; exit 0; }

# 1. 备份
ts=$(date +%s)
cp "$SESS" "$SESS.bak.$ts" || fail backup 1
cp "$CHIEF_DB" "$CHIEF_DB.bak.$ts" || fail backup_db 1

# 2. 先停网关再改文件：网关运行时内存副本会在下次保存时覆盖我们的编辑，
#    停掉后再写就没有竞争者（停机约 15 秒，看板已确认空闲）。
systemctl --user stop hermes-gateway || fail stop 1
for i in $(seq 1 20); do
    systemctl --user is-active --quiet hermes-gateway || break
    sleep 1
done
systemctl --user is-active --quiet hermes-gateway && fail stop_timeout 1
STOPPED=1

# 3. 写 suspended（sessions.json 与 state.db gateway_routing 两处都写）
python3 - "$SESS" "$CHIEF_DB" "$KEY" <<'PY' || fail mark_suspended 1
import json, os, sqlite3, sys, tempfile
sess, db, key = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.load(open(sess))
ent = d.get(key)
if not isinstance(ent, dict):
    print("entry not found", file=sys.stderr); sys.exit(1)
ent["suspended"] = True
ent["resume_pending"] = False
ent["suspended_reason"] = "manual_rotation_paimeng_2026-10-30"
d[key] = ent
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(sess))
with os.fdopen(fd, "w") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
os.replace(tmp, sess)
try:
    con = sqlite3.connect(db, timeout=20)
    rows = con.execute("select session_key, entry_json from gateway_routing").fetchall()
    for k, ej in rows:
        if k != key:
            continue
        e = json.loads(ej)
        e["suspended"] = True
        e["suspended_reason"] = "manual_rotation_paimeng_2026-10-30"
        con.execute("update gateway_routing set entry_json=? where session_key=?", (json.dumps(e, ensure_ascii=False), k))
    con.commit(); con.close()
except Exception as exc:
    print(f"routing update skipped: {exc}", file=sys.stderr)
print("marked suspended")
PY

# 4. 启动网关（刚才 stop 过，这里 start；stop/start 比 restart 更能保证编辑窗口）
systemctl --user start hermes-gateway || fail start 1
for i in $(seq 1 30); do
    systemctl --user is-active --quiet hermes-gateway && break
    sleep 2
done
systemctl --user is-active --quiet hermes-gateway || fail restart_not_active 1

# 4. 复核：文件里真的还是 suspended（网关启动后不应覆盖它）
sleep 5
state=$(python3 - "$SESS" "$KEY" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
e = d.get(sys.argv[2], {})
print(f"{e.get('suspended')}|{e.get('session_id')}")
PY
)
log "after restart: $state"
case "$state" in
    True*) : ;;
    *) fail verify_suspended 1 "got=$state" ;;
esac

touch "$MARK"
log "OK: chief session suspended; next message to chief starts a fresh session"
crontab -l 2>/dev/null | grep -v "$(basename "$SELF")" | crontab - || true
exit 0
