#!/usr/bin/env bash
# 18:03 恢复看板派工（老板定：高峰计费期间停派，18:03 开工）。
# 做四件事：① dispatch_interval_seconds 86400 → 60；② 重启网关让配置生效；
#           ③ 自检（status / dispatch --dry-run）；④ 对账「config 声明 vs 运行态」。
# 跑完自己从 crontab 摘掉（一次性）。
# 失败绝不静默：任一 `hermes` 调用 rc≠0 或对账 MISMATCH → 写 /tmp/dispatch-resume.FAILED 并 exit 非 0。
#   —— 历史坑 1：裸 `hermes` 在 cron PATH 下 rc=127（2026-10-29 静默失败 4h13m）
#   —— 历史坑 2：echo "$(date …) rc=$?" 里 $? 被命令替换吞成 0
set -u

export PATH=/home/liu/.local/bin:/usr/local/bin:/usr/bin:/bin
HERMES=/home/liu/.local/bin/hermes
CFG=/home/liu/.hermes/config.yaml
GWLOG=/home/liu/.hermes/logs/gateway.log
LOG=/tmp/resume_dispatch_1803.log
FAILMARK=/tmp/dispatch-resume.FAILED
SELF=/home/liu/.hermes/profiles/paimeng/scripts/resume_dispatch_1803.sh

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

echo "$(date '+%F %T') resume start" >> "$LOG"

# ① 恢复间隔
python3 - "$CFG" >> "$LOG" 2>&1 <<'PY'
import sys, re
p = sys.argv[1]
s = open(p).read()
new = re.sub(r"(\n\s*dispatch_interval_seconds:\s*)86400", r"\g<1>60", s)
if new == s:
    print("WARN: 未找到 dispatch_interval_seconds: 86400（可能已被改过）")
open(p, "w").write(new)
PY
rc=$?
echo "$(date '+%F %T') cfg update rc=$rc" >> "$LOG"
[ "$rc" -eq 0 ] || fail "cfg update" "$rc" "config=$(read_cfg)" "running=$(read_run)"
grep -n "dispatch_interval_seconds" "$CFG" >> "$LOG"

# ② 摘掉自己
crontab -l 2>/dev/null | grep -v "resume_dispatch_1803.sh" > /tmp/.ct2.$$ && crontab /tmp/.ct2.$$ 2>>"$LOG"
rm -f /tmp/.ct2.$$

# ③ 重启网关并自检
timeout 150 "$HERMES" gateway restart >> "$LOG" 2>&1; rc=$?
echo "$(date '+%F %T') restart rc=$rc" >> "$LOG"
[ "$rc" -eq 0 ] || fail "gateway restart" "$rc" "config=$(read_cfg)" "running=$(read_run)"

sleep 10

"$HERMES" gateway status >> "$LOG" 2>&1; rc=$?
echo "$(date '+%F %T') gateway status rc=$rc" >> "$LOG"
[ "$rc" -eq 0 ] || fail "gateway status" "$rc" "config=$(read_cfg)" "running=$(read_run)"

timeout 90 "$HERMES" kanban --board devteam dispatch --dry-run --json >> "$LOG" 2>&1; rc=$?
echo "$(date '+%F %T') dispatch dry-run rc=$rc" >> "$LOG"
[ "$rc" -eq 0 ] || fail "dispatch dry-run" "$rc" "config=$(read_cfg)" "running=$(read_run)"

# ④ 对账：config 声明 vs 运行态（最多等 30s 让新 startup 行落盘，超时即判 MISMATCH）
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
echo "$(date '+%F %T') resume done" >> "$LOG"
exit 0
