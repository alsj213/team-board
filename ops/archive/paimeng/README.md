# paimeng 历史脚本

这些文件 2026-11-09 从 `~/.hermes/profiles/paimeng/scripts/` 移出，只在这份归档里保留。

移除判据：

- 没有任何启用的 hermes cron 任务或 crontab 行引用它们；
- 其中 `core_patch_check.py`、`daily_metrics.py`、`kanban_autoclose.py`、
  `kanban_status.py`、`kanban_unstick.py`、`pr_orphan_autoclose.py`、
  `pr_orphan_check.sh`、`tb_serve_watchdog.sh` 与 `ops/scripts/ops/` 下的版本重复，
  且多数已落后；
- 其余是一次性操作（重启网关、暂停/恢复派工、轮换会话），不是常驻能力。

需要再用时从本目录取回，不要直接改线上副本。
