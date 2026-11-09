# 管理面（唯一源）

这里的文件是运维规则与看护脚本的唯一源。运行中的副本在 `~/.hermes/profiles/` 与
`~/.config/systemd/user/`，由 `team-board mgmt install` 写入，`team-board mgmt check`
比对。两边内容不同就是漂移。

## 布局

```text
ops/
  playbook.md              # 团队规则唯一源（ops 维护）
  scripts/ops/             # ops profile 的看护脚本
  scripts/paimeng/         # paimeng profile 仍在用的脚本
  systemd/                 # team-board systemd timer/service
  archive/paimeng/         # 已从线上移除的 paimeng 一次性脚本（只留历史）
  manifest.json            # 源 -> 安装件映射（scripts/systemd 必须全部登记）
  schedules.json           # crontab 看护件、systemd timer、hermes cron 任务清单
```

## 命令

```bash
team-board mgmt check                 # 0 一致 / 1 漂移 / 2 环境错误
team-board mgmt check --files-only    # 不读 crontab / jobs.json
team-board mgmt check --schedules-only
team-board mgmt check --json

team-board mgmt install --dry-run     # 先看会写哪些
team-board mgmt install               # 目标已存在且内容不同时拒绝
team-board mgmt install --force       # 覆盖前备份到 ~/.cache/team-board/mgmt-backup/<UTC>/
team-board mgmt capture               # 把线上内容反向写回仓库
```

`--root` 指定仓库根（默认 `TEAM_BOARD_ROOT` 或按包位置反推），`--home` 指定目标根
（默认当前用户 home）。

## 改东西的路径

- **脚本 / systemd unit**：改仓库 `ops/` 下的源，跑 `mgmt install`，再跑 `mgmt check`。
  不要在 `~/.hermes` 里直接改脚本 —— 下次 `check` 会红。
- **playbook**：ops 仍可直接改线上 `~/.hermes/profiles/ops/workspace/playbook.md`；
  改完跑 `team-board mgmt capture` 把内容写回仓库，再由 repo 走提交。没 capture 之前
  `mgmt check` 会报 `content-drift`。
- **systemd**：`mgmt install` 只写文件；生效还要
  `systemctl --user daemon-reload`。timer 的 enable/disable 不在 `install` 里自动做，
  `mgmt check` 会报 `timer-disabled`。

## 计划清单

`schedules.json` 是调度面的清单，防止三套调度器（系统 crontab、systemd timer、
hermes cron）各跑各的：

- `crontab`：`guard_run.sh <guard> <period> <interpreter> <script> [args]` 的期望集合；
  多一条报 `crontab-extra`，少一条报 `crontab-missing`，周期/脚本/参数变了报
  `crontab-drift`。
- `systemd_timers`：timer 文件存在且已 enable；多一个 `team-board-*.timer` 报
  `timer-extra`。
- `hermes_jobs`：按 profile + name 比对 schedule/script/enabled；多一个任务报
  `hermes-job-extra`。

## 不管什么

- `SOUL.md` 章程：hatch 维护，改动走 `charter-log.md`；不在本目录。
- `skills/`、`memories/`、workspace 日志与 `guard/*.ok`：运行时状态，不版本化。
- `config.yaml` / `auth.json`：含机器身份或密钥，不入库。

## 2026-11-09 迁移记录

- ops 20 个脚本、paimeng 仍在用的 5 个脚本、12 个 systemd unit、playbook 入库。
- paimeng 的 21 个历史脚本移出线上，源保留在 `archive/paimeng/`；其中 8 个是 ops
  脚本的旧副本，其余是一次性重启/暂停脚本。
- paimeng 的两个停用任务（看板自愈、卡死告警）与 ops 重复，迁移后删除。
