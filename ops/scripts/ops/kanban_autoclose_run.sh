#!/usr/bin/env bash
# 包装器：给 cron --no-agent 调用，固定对 devteam 板执行自动关父卡 + 滞留标记。
# 输出只在有动作时非空（cron 空输出即静默）。
exec python3 "$HOME/.hermes/profiles/ops/scripts/kanban_autoclose.py" --board devteam --apply --stale-minutes 30
