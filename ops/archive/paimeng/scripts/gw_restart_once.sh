#!/usr/bin/env bash
# 一次性任务：等 devteam 看板空闲（running=0）后重启网关，让 dispatcher 的
# active_pr 守卫修复生效。由 cron 每分钟调用（在网关进程之外，否则会被 SIGTERM 打死）。
# 触发后自己从 crontab 里摘掉，避免重复。
# 失败绝不静默：任一 `hermes` 调用 rc≠0 或对账 MISMATCH → 写 /tmp/dispatch-resume.FAILED 并 exit 非 0。
#   —— 历史坑 1：裸 `hermes` 在 cron PATH 下 rc=127（2026-10-29 静默失败 4h13m）
#   —— 历史坑 2：echo "$(date …) rc=$?" 里 $? 被命令替换吞成 0
set -u

export PATH=/home/liu/.local/bin:/usr/local/bin:/usr/bin:/bin
HERMES=/home/liu/.local/bin/hermes
CFG=/home/liu/.hermes/config.yaml
GWLOG=/home/liu/.hermes/logs/gateway.log
DB=/home/liu/.hermes/kanban/boards/devteam/kanban.db
LOG=/tmp/gw_restart_once.log
SELF=/home/liu/.hermes/profiles/paimeng/scripts/gw_restart_once.sh
LOCK=/tmp/.gw_restart_once.lock
FAILMARK=/tmp/dispatch-resume.FAILED

read_cfg() { grep -E '^[[:space:]]*dispatch_interval_seconds:' "$CFG" | awk '{print $2}'; }
read_run() { grep "embedded in gateway" "$GWLOG" | tail -1 | sed -E 's/.*interval=([0-9.]+)s.*/\1/'; }

# fail <step> <rc> [附加 kv...] —— 写 marker（时间戳/哪一步/rc/config/running）并以非零退出
fail() {
    _step=$1; _rc=$2; shift 2
    {
        echo "time=$(date '+%F %T')"
        echo "script=$(basename "$0")"
        echo "step=$_step"
        echo "rc=$_rc"
        for kv in "$@"; do echo "$kv"; done
    } > "$FAILMARK"
    echo "$(date '+%F %T') FAILED step=$_step rc=$_rc $*" >> "$LOG"
    [ "$_rc" -ne 0 ] || _rc=1
    exit "$_rc"
}

running=$(python3 - "$DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print(con.execute("select count(*) from tasks where status='running'").fetchone()[0])
PY
)
[ "$running" = "0" ] || exit 0

# 只做一次
mkdir "$LOCK" 2>/dev/null || exit 0

# 先摘掉 cron 行，再做重启
crontab -l 2>/dev/null | grep -v "gw_restart_once.sh" > /tmp/.ct.$$ && crontab /tmp/.ct.$$ 2>>"$LOG"
rm -f /tmp/.ct.$$

echo "$(date '+%F %T') board idle → restarting gateway (pid $(pgrep -f 'hermes.*gateway' | head -1))" >> "$LOG"

timeout 150 "$HERMES" gateway restart >> "$LOG" 2>&1; rc=$?
echo "$(date '+%F %T') restart rc=$rc" >> "$LOG"
[ "$rc" -eq 0 ] || fail "gateway restart" "$rc" "config=$(read_cfg)" "running=$(read_run)"

sleep 10

st=$(timeout 60 "$HERMES" gateway status 2>&1); rc=$?
echo "$(date '+%F %T') gateway status rc=$rc: $(printf '%s' "$st" | head -2 | tr '\n' ' ')" >> "$LOG"
[ "$rc" -eq 0 ] || fail "gateway status" "$rc" "config=$(read_cfg)" "running=$(read_run)"

timeout 90 "$HERMES" kanban --board devteam dispatch --dry-run --json >> "$LOG" 2>&1; rc=$?
echo "$(date '+%F %T') dispatch dry-run rc=$rc" >> "$LOG"
[ "$rc" -eq 0 ] || fail "dispatch dry-run" "$rc" "config=$(read_cfg)" "running=$(read_run)"

# 对账：config 声明 vs 运行态（最多等 30s 让新 startup 行落盘，超时即判 MISMATCH）
CFG_VAL=$(read_cfg)
RUN_VAL=$(read_run)
_i=0
while [ "$CFG_VAL" != "${RUN_VAL%.0}" ] && [ "$_i" -lt 30 ]; do
    sleep 1
    _i=$((_i + 1))
    RUN_VAL=$(read_run)
done
echo "$(date '+%F %T') reconcile config=$CFG_VAL running=$RUN_VAL" >> "$LOG"
if [ "$CFG_VAL" != "${RUN_VAL%.0}" ]; then
    echo "$(date '+%F %T') RECONCILE MISMATCH config=$CFG_VAL running=$RUN_VAL" >> "$LOG"
    fail "reconcile" 3 "config=$CFG_VAL" "running=$RUN_VAL"
fi

rm -f "$FAILMARK"
echo "$(date '+%F %T') post-restart dry-run logged; restart done" >> "$LOG"
exit 0
