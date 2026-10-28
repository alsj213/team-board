# team-board

只读查看 devteam 看板的命令行工具：运行时零第三方依赖（纯标准库），绝不写库。

## 数据源

`/home/liu/.hermes/kanban/boards/devteam/kanban.db` —— SQLite，以 `mode=ro`
**只读打开**，绝不写入、绝不建表；只读 `tasks` 与 `task_events` 两张表。
文件不存在或不是合法 SQLite 库时，命令以退出码 1 结束并在 stderr 打印
`错误: ...`，不会自动创建文件。

## 用法

```
team-board list
team-board summary [--date 2026-10-28]
team-board member engineer
team-board export
```

- `list` —— 未归档卡的总览表（卡号 / 负责人 / 状态 / 耗时(分) / 标题），
  按状态顺序（triage → todo → scheduled → ready → running → blocked →
  review → done）→ 同状态内耗时降序 → id 升序；`archived` 不显示。
- `summary` —— 当日汇总：完成 / 在跑 / 阻塞 三个计数 + 超时异常清单
  （`running` 超过 60 分钟、`review` 超过 30 分钟）。
- `member <名字>` —— 单个成员的交付数、被驳回次数、平均耗时：

  ```
  成员: engineer
  交付: 1
  被驳回: 2
  平均耗时: 25 分钟（样本 1 张已交付卡）
  ```

  没有已交付卡时最后一行是 `平均耗时: —（无已交付卡）`；看板上没有这个
  负责人时退出码 1。
- `export` —— 未归档卡的 JSON 快照，给下游脚本读，排序与 `list` 完全一致：

  ```
  [{"id": "t_a", "assignee": "engineer", "status": "running",
    "duration_minutes": 75, "title": "…"}]
  ```

  实际输出是**单行**（无 indent、无 sort_keys），`ensure_ascii=False`；每个
  对象的键顺序固定 `id, assignee, status, duration_minutes, title`，类型恒为
  `str, str, str, int, str`（`assignee` / `status` / `title` 为空时是空串，
  不是 `list` 里的 `—`），标题不截断。没有任何未归档卡时输出 `[]`、退出码 0。

## 通用参数

两个参数四个子命令都支持：

- `--db PATH` —— 看板库路径，默认
  `/home/liu/.hermes/kanban/boards/devteam/kanban.db`。
- `--now EPOCH` —— 基准时刻（epoch 秒），默认当前时间。所有「算到此刻」
  的耗时都以它为准；固定成 fixture 的 `1793160000` 即可得到可重复的输出。

`summary` 另有 `--date YYYY-MM-DD`（默认取 `--now` 所在日期）。

## 退出码

- `0` —— 正常输出。
- `1` —— 数据源有问题（不存在 / 打不开 / 不是合法库）或人名查无此人；
  stderr 以 `错误: ` 开头，stdout 为空。
- `2` —— 参数错误（未知子命令、`--date` 格式非法、`member` 缺名字等），
  由 argparse 报错。

## 口径

- **耗时**：从开始到结束的分钟数，向下取整。开始取 `started_at`，缺省回退
  `created_at`；结束取 `completed_at`，未结束则算到 `--now`；结束早于开始的
  脏数据夹到 0。
- **交付数**：`status == "done"` 且负责人是该成员的卡数；`archived` 不算交付。
- **被驳回**：`task_events` 里 `kind = 'changes_requested'` 的事件次数
  （一次打回一行事件；不看状态，含 `archived`，同名负责人的卡全部计入）。
- **平均耗时**：上述已交付卡的耗时之和 // 样本数，向下取整；样本为 0 时不
  做除法。只按负责人名字匹配，不做别名归一。
- **summary 的 `--date`**：只影响「完成数」（按 `completed_at` 的本地自然日
  比对）；在跑 / 阻塞 / 异常永远描述 `--now` 那一刻的快照。

## 开发

需要 Python >= 3.11。

```
python3 -m venv .venv && .venv/bin/pip install -e . && .venv/bin/python -m pytest -q
```

跑真实库看效果：

```
.venv/bin/team-board list
.venv/bin/team-board summary
.venv/bin/team-board member engineer
.venv/bin/team-board export
```

## CI 验证

本行由 pm 于 2026-10-28 加入，用于确认新 PR 会真的触发 GitHub Actions check（验证用的小 PR，看完即关，不合并）。
