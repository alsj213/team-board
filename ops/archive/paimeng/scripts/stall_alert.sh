#!/usr/bin/env bash
# cron 包装器：cron 的 --script 不接受参数，逻辑写在 stall_alert.py
exec /usr/bin/python3 /home/liu/.hermes/profiles/paimeng/scripts/stall_alert.py
