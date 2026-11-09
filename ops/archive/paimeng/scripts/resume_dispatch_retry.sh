#!/usr/bin/env bash
# 18:03 的恢复脚本又踩了同一个坑（cron PATH 里没有 hermes），配置改回 60 了但网关没重启，
# 所以派工仍是暂停状态。本脚本用绝对路径重启网关，让 interval=60 生效。
# 失败绝不静默：任一 `hermes` 调用 rc≠0 或对账 MISMATCH → 写 /tmp/dispatch-resume.FAILED 并 exit 非 0。
#   —— 历史坑：echo "$(date …) rc=$?" 里 $? 被命令替换吞成 0（PATH/HERMES 本来是对的，rc 仍是假的）
set -u

export PATH=/home/liu/.local/bin:/usr/local/bin:/usr/bin:/bin
HERMES=/home/liu/.local/bin/hermes
CFG=/home/liu/.hermes/config.yaml
GWLOG=/home/liu/.hermes/logs/gateway.log
LOG=/tmp/resume_dispatch_retry.log
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

echo "$(date '+%F %T') resume retry start" >> "$LOG"

crontab -l 2>/dev/null | grep -v "resume_dispatch_retry.sh" > /tmp/.ct5.$$ && crontab /tmp/.ct5.$$ 2>>"$LOG"
rm -f /tmp/.ct5.$$

grep -n "dispatch_interval_seconds" "$CFG" >> "$LOG"

timeout 150 "$HERMES" gateway restart >> "$LOG" 2>&1; rc=$?
echo "$(date '+%F %T') restart rc=$rc" >> "$LOG"
[ "$rc" -eq 0 ] || fail "gateway restart" "$rc" "config=$(read_cfg)" "running=$(read_run)"

sleep 12

"$HERMES" gateway status >> "$LOG" 2>&1; rc=$?
echo "$(date '+%F %T') gateway status rc=$rc" >> "$LOG"
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
echo "$(date '+%F %T') resume retry done" >> "$LOG"
exit 0
