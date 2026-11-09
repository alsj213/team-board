#!/usr/bin/env bash
# patrol_quick 的 guard 包装（ops 的 hermes cron，no-agent，每小时 :40 跑一次）
#
# 为什么不是直接 `guard_run.sh patrol_quick 60 patrol_quick.py`：
#   guard_run.sh 把 stdout 收进变量写日志，cron 就拿不到命中文本、无法投给 ops
#   （与 stall_alert 同一个限制，见 stall_alert_guard.sh 的注释）。
# 折中：仍然走 guard_run.sh 写心跳（guard/patrol_quick.ok，周期 60 分钟 →
#   guard_health 阈值 125 分钟）；命中文本另存两份：
#     ① stdout —— cron 把它存进 cron/output/<job>/<ts>.md（本地投递）；
#     ② workspace/patrol-hits.md —— ops 的收件箱，**整份覆盖**，只留最近一次的命中情况；
#        ops 的 agent 巡检（01:10 / 12:10 / 07:40）第一步读它。
#   （ops 这个 profile 没配 feishu：`--deliver feishu:…` 实测投不出去 ——
#    `platform 'feishu' not configured/enabled`，2026-10-31 11:40 实跑，见 charter-log。）
# 沉默即正常：无命中时 stdout 零输出，cron 不发消息；收件箱里只留一行「无命中」。
set -u

export PATH=/home/liu/.local/bin:/usr/local/bin:/usr/bin:/bin

S=/home/liu/.hermes/profiles/ops/scripts
GDIR=/home/liu/.hermes/profiles/ops/workspace/guard
WS=/home/liu/.hermes/profiles/ops/workspace
HITS="$GDIR/patrol_quick.hits"
INBOX="$WS/patrol-hits.md"

mkdir -p "$GDIR"
rm -f "$HITS"

"$S/guard_run.sh" patrol_quick 60 \
  /usr/bin/python3 "$S/patrol_quick.py" --hits-out "$HITS"

if [ -s "$HITS" ]; then
  cp "$HITS" "$INBOX"
  cat "$HITS"
  rm -f "$HITS"
else
  printf '%s 快检无命中（六条 SQL 全空）\n' "$(date '+%F %H:%M')" > "$INBOX"
fi

exit 0
