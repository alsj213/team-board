#!/usr/bin/env bash
# 13:52 硬暂停：不等到空闲，直接重启网关让 dispatch_interval_seconds=86400 生效。
# 为什么硬暂停：网关的派工间隔只在启动时读一次，不重启就继续每 60 秒拉新卡，
# 看板永远不会空闲（老板 14:00 起按高峰计费，不能在 14:00 后还在跑）。
# 在跑的卡会被 reclaim（不丢），18:03 恢复后继续。
# 失败绝不静默：任一 `hermes` 调用 rc≠0 或对账 MISMATCH → 写 /tmp/dispatch-resume.FAILED 并 exit 非 0。
#   —— 历史坑 1：裸 `hermes` 在 cron PATH 下 rc=127（13:58 那次只是 agent shell 拉起才侥幸成功）
#   —— 历史坑 2：echo "$(date …) rc=$?" 里 $? 被命令替换吞成 0
set -u

export PATH=/home/liu/.local/bin:/usr/local/bin:/usr/bin:/bin
HERMES=/home/liu/.local/bin/hermes
CFG=/home/liu/.hermes/config.yaml
GWLOG=/home/liu/.hermes/logs/gateway.log
LOG=/tmp/pause_dispatch_1352.log
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

echo "$(date '+%F %T') hard pause start" >> "$LOG"

# 摘掉空闲重启那条（避免重复重启）和自己
crontab -l 2>/dev/null | grep -v -e "gw_restart_once.sh" -e "pause_dispatch_1352.sh" > /tmp/.ct3.$$ && crontab /tmp/.ct3.$$ 2>>"$LOG"
rm -f /tmp/.ct3.$$

running=$(python3 - <<'PY'
import sqlite3
con = sqlite3.connect("file:/home/liu/.hermes/kanban/boards/devteam/kanban.db?mode=ro", uri=True)
print(con.execute("select count(*) from tasks where status='running'").fetchone()[0])
PY
)
echo "$(date '+%F %T') running before restart = $running" >> "$LOG"

timeout 150 "$HERMES" gateway restart >> "$LOG" 2>&1; rc=$?
echo "$(date '+%F %T') restart rc=$rc" >> "$LOG"
[ "$rc" -eq 0 ] || fail "gateway restart" "$rc" "config=$(read_cfg)" "running=$(read_run)"

sleep 10

"$HERMES" gateway status >> "$LOG" 2>&1; rc=$?
echo "$(date '+%F %T') gateway status rc=$rc" >> "$LOG"
[ "$rc" -eq 0 ] || fail "gateway status" "$rc" "config=$(read_cfg)" "running=$(read_run)"

grep -n "dispatch_interval_seconds" "$CFG" >> "$LOG"

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
echo "$(date '+%F %T') hard pause done (18:03 由 resume_dispatch_1803.sh 恢复)" >> "$LOG"
exit 0
