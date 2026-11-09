#!/usr/bin/env bash
# 看板自愈的 guard 包装（供 ops 的 hermes cron 每 2 分钟调用，产出 selfheal.ok 心跳）
exec /home/liu/.hermes/profiles/ops/scripts/guard_run.sh selfheal 2 \
  /usr/bin/python3 /home/liu/.hermes/profiles/ops/scripts/kanban_autoclose.py \
  --board devteam --apply --stale-minutes 30
