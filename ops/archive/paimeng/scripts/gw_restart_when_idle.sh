#!/usr/bin/env bash
# 等看板空闲后重启网关 —— 让 kanban_db_dispatch 的 active_pr 守卫修复生效。
# 背景：网关在启动时加载模块，改代码后必须重启才生效；
#      pm 有卡在跑时重启会打断（用户选 B：等它们跑完）。
# 用法：后台跑，日志写 /tmp/gw_restart_when_idle.log
# 失败绝不静默：任一 `hermes` 调用 rc≠0、对账 MISMATCH、或等满 MAX_WAIT_MIN 仍未空闲
#               → 写 /tmp/dispatch-resume.FAILED 并 exit 非 0。
#   —— 历史坑 1：裸 `hermes` 在 cron PATH 下 rc=127（2026-10-29 静默失败 4h13m）
#   —— 历史坑 2：echo "$(date …) rc=$?" 里 $? 被命令替换吞成 0
set -u

export PATH=/home/liu/.local/bin:/usr/local/bin:/usr/bin:/bin
HERMES=/home/liu/.local/bin/hermes
CFG=/home/liu/.hermes/config.yaml
GWLOG=/home/liu/.hermes/logs/gateway.log
DB=/home/liu/.hermes/kanban/boards/devteam/kanban.db
LOG=/tmp/gw_restart_when_idle.log
FAILMARK=/tmp/dispatch-resume.FAILED
MAX_WAIT_MIN=${MAX_WAIT_MIN:-240}      # 最多等 4 小时
INTERVAL=60

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

echo "$(date '+%F %T') watcher started (max ${MAX_WAIT_MIN}m)" >> "$LOG"
waited=0
while [ "$waited" -lt "$MAX_WAIT_MIN" ]; do
  running=$(python3 - "$DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print(con.execute("select count(*) from tasks where status='running'").fetchone()[0])
PY
)
  if [ "$running" = "0" ]; then
    echo "$(date '+%F %T') board idle (running=0) after ${waited}m → restarting gateway" >> "$LOG"
    timeout 120 "$HERMES" gateway restart >> "$LOG" 2>&1; rc=$?
    echo "$(date '+%F %T') gateway restart rc=$rc" >> "$LOG"
    [ "$rc" -eq 0 ] || fail "gateway restart" "$rc" "config=$(read_cfg)" "running=$(read_run)"

    # 重启后验证：新代码是否加载（该卡应不再是 respawn_guarded active_pr）
    sleep 8
    timeout 60 "$HERMES" kanban --board devteam dispatch --dry-run --json >> "$LOG" 2>&1; rc=$?
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
    echo "$(date '+%F %T') post-restart dry-run logged; watcher done" >> "$LOG"
    exit 0
  fi
  sleep "$INTERVAL"
  waited=$((waited + 1))
done

fail "timeout waiting idle" 2 "waited=${MAX_WAIT_MIN}m" "board_running=$running" "config=$(read_cfg)" "running=$(read_run)"
