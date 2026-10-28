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

## Web 看板（serve）

一行启动，然后用浏览器打开 `http://127.0.0.1:8787/`：

```
team-board serve
```

参数（`serve` 只认这三个，**不接受 `--now`** —— 快照时刻取服务进程的真实时钟）：

| 参数 | 取值 | 说明 |
| --- | --- | --- |
| `--port PORT` | 1–65535，默认 `8787` | 监听端口；`0`、负数、`65536`、`abc` 一律 argparse 报错、退出码 2 |
| `--db PATH` | 同其它子命令 | 看板 SQLite 路径，默认 `/home/liu/.hermes/kanban/boards/devteam/kanban.db` |
| `--bind ADDR` | 只允许 `127.0.0.1` / `localhost`，默认 `127.0.0.1` | 其它值（含 `0.0.0.0`、空串）退出码 2；**不支持 0.0.0.0 / 对外暴露 / IPv6** |

页面行为：

- 顶部一行统计（完成 / 在跑 / 阻塞 / 异常），下面四列 `running` / `queued` /
  `blocked` / `done`，卡上显示卡号、负责人、耗时（分）、标题。
- 每 5 秒自动刷新，右上角开关可关闭自动刷新（关掉后只能手动重载页面）。
- 纯前端 `fetch` 两个 JSON 接口，无 CDN、无第三方 JS/CSS、无构建步骤，
  页面内容全部由服务端内联的单个 HTML 提供。

两个接口：

```
GET /api/board
{"columns": {"running": [...], "queued": [...], "blocked": [...], "done": [...]}}

// 每张卡的键顺序固定
{"id": "t_a", "assignee": "engineer", "status": "running", "duration_minutes": 75, "title": "…"}

GET /api/summary
{"snapshot": "2026-10-28 21:25", "date": "2026-10-28", "completed": 1, "running": 2,
 "blocked": 1, "anomaly": 2,
 "anomalies": [{"status": "running", "minutes": 75, "threshold": 60, "id": "t_a", "title": "…"}]}
```

实际响应是**单行 JSON**（无 indent、无 sort_keys），`ensure_ascii=False`（中文原样，
不转义成 `\uXXXX`）；键顺序与上面逐字一致。`/api/summary` 与 `team-board summary`
子命令**同口径**（共用同一份 `commands.summary_stats`），异常清单只算 `running`
超过 60 分钟、`review` 超过 30 分钟的卡。

分组口径：

- `running` ← `running`；`queued` ← `ready` + `scheduled`；`blocked` ← `blocked`；
  `done` ← `done`。
- **`archived` / `triage` / `todo` / `review` 的卡不出现在任何一列**（`review`
  只在 `/api/summary` 的异常清单里以超时条目的形式出现）。

只读与安全边界：

- 库以 `mode=ro` 打开，每个请求现开现关；**服务没有任何写接口**（非 GET 一律
  405 + `{"error": "method not allowed"}`），未知路径 404。
- 只绑本机回环地址、无鉴权、无多用户：**设计上不对公网暴露**，不要用反向代理
  或 `--bind 0.0.0.0` 把它放到外网。

退出码：

- `0` —— 正常跑（Ctrl-C / SIGINT 收尾也是 0）。
- `1` —— 库不可读（不存在 / 打不开）、端口被占用或无权监听；stderr 以 `错误: `
  开头，如 `错误: 数据源不存在: …`、`错误: 无法监听 127.0.0.1:8787: …`。
- `2` —— 参数错误（`--port` 越界/非整数、`--bind` 非本机地址、未知 flag），
  由 argparse 报错。

已知限制：`/api/summary` 的「完成」按**服务进程本地时区**的自然日计数（与
`summary` 子命令一致）；耗时与超时判断用进程真实时钟，因此用固定 `--now` 建的
fixture 库隔久了再打开，越界卡会变多 —— 这是真实时钟的本意，不是 bug。

## 通用参数

两个参数四个子命令都支持：

- `--db PATH` —— 看板库路径，默认
  `/home/liu/.hermes/kanban/boards/devteam/kanban.db`。
- `--now EPOCH` —— 基准时刻（epoch 秒），默认当前时间。所有「算到此刻」
  的耗时都以它为准；固定成 fixture 的 `1793160000` 即可得到可重复的输出。
  取值必须是本平台 `datetime.fromtimestamp()` 能表示的 epoch 秒，负数合法
  （表示 1970 年之前，不拦）；13 位毫秒（`1793160000000`）、天文数字
  （`99999999999999`）、非整数（`abc`）与空串都算越界，由 argparse 报错、
  退出码 2，stderr 含 `基准时间超出可表示范围`；实测：

  ```
  team-board list --now 1793160000000     # 退出码 2
  team-board list --now 99999999999999    # 退出码 2
  team-board list --now -86400            # 退出码 0，正常输出
  ```

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
