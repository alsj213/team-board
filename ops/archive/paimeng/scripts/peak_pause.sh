#!/usr/bin/env bash
# 高峰停派：重启网关让 dispatch_interval_seconds=86400 生效（配置只在启动时读一次）。
# 由 cron 调用（必须在网关进程之外，否则重启会杀掉自己）。
set -u
export PATH=/home/liu/.local/bin:/usr/local/bin:/usr/bin:/bin
HERMES=/home/liu/.local/bin/hermes
LOG=/tmp/peak_pause.log
echo "$(date '+%F %T') peak pause start" >> "$LOG"
crontab -l 2>/dev/null | grep -v -e "peak_pause.sh" -e "peak_resume.sh" > /tmp/.pk.$$ && crontab /tmp/.pk.$$ 2>>"$LOG"; rm -f /tmp/.pk.$$

# 立即装回 12:03 的恢复任务
( crontab -l 2>/dev/null; \
  echo "3 12 * * 1-5 PATH=/home/liu/.local/bin:/usr/bin:/bin /home/liu/.hermes/profiles/paimeng/scripts/peak_resume.sh >> /tmp/peak_resume.log 2>&1" ) | crontab -

timeout 150 "$HERMES" gateway restart >> "$LOG" 2>&1
echo "$(date '+%F %T') restart rc=$?" >> "$LOG"
sleep 10
grep -n dispatch_interval_seconds /home/liu/.hermes/config.yaml >> "$LOG"
echo "$(date '+%F %T') peak pause done（12:03 自动恢复）" >> "$LOG"
