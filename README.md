# team-board

只读查看 devteam 看板的命令行工具：运行时零第三方依赖（纯标准库），绝不写库。

## 数据源

`/home/liu/.hermes/kanban/boards/devteam/kanban.db` —— SQLite，以 `mode=ro`
**只读打开**，绝不写入、绝不建表。`tasks` 是必读表；`task_events` 只有
`member`（被驳回次数）和 `summary`（沿用既有的加载路径，`render_summary`
并不使用它的值）会读。**Web 的两个接口都不读 `task_events`**：缺这张表时
`/api/board` 与 `/api/summary` 都照常 200，而命令行 `member` / `summary`
按数据源错误退出（退出码 1）。
文件不存在或不是合法 SQLite 库时，命令以退出码 1 结束并在 stderr 打印
`错误: ...`，不会自动创建文件。

## 用法

```
team-board list
team-board summary [--date 2026-10-28]
team-board member engineer
team-board export
```

共八个子命令：`list` / `summary` / `member` / `export` / `branch-audit` / `merge-audit` /
`merge-record` / `serve`（`serve` 见下面「Web 看板」一节，`branch-audit` 见「分支稽核」一节，
`merge-audit` / `merge-record` 见「合并认账稽核」一节）。

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
| `--port PORT` | 1–65535 的**纯 ASCII 十进制数字**，默认 `8787` | 监听端口；`0`、负数、`65536`、`abc` 一律 argparse 报错、退出码 2。只认 `0-9`：`" 8787"`（前导空白）、`"+8787"`、`"1_000"`、`"١٢٣"`（非 ASCII 数字，`int()` 会当成 123）同样退出码 2 |
| `--db PATH` | 同其它子命令 | 看板 SQLite 路径，默认 `/home/liu/.hermes/kanban/boards/devteam/kanban.db` |
| `--bind ADDR` | 只允许 `127.0.0.1` / `localhost`，默认 `127.0.0.1` | 其它值（含 `0.0.0.0`、空串）退出码 2；**不支持 0.0.0.0 / 对外暴露 / IPv6** |

页面行为：

- 顶部一行统计（完成 / 在跑 / 阻塞 / 异常），下面四列 `running` / `queued` /
  `blocked` / `done`，卡上显示卡号、负责人、耗时（分）、标题。
- 每 5 秒自动刷新，右上角开关可关闭自动刷新（关掉后只能手动重载页面）。
  间隔只有一处来源：`web.py` 的 `REFRESH_MS`，页面里的 `const REFRESH_MS`
  由它渲染（改常量页面就跟着变）。
- 刷新失败（任一接口非 2xx 或网络层失败）时**保留上一帧**，在统计行下方显示
  一行 `刷新失败（HTTP <状态码>）：<原因>，保留上一帧`，成功一次后自动消失。
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
超过 60 分钟、`review` 超过 30 分钟的卡；响应里**没有** `rework` 字段（本接口
不读 `task_events`，缺这张表也照常 200）。

分组口径：

- `running` ← `running`；`queued` ← `ready` + `scheduled`；`blocked` ← `blocked`；
  `done` ← `done`。
- **`archived` / `triage` / `todo` / `review` 的卡不出现在任何一列**（`review`
  只在 `/api/summary` 的异常清单里以超时条目的形式出现）。

只读与安全边界：

- 库以 `mode=ro` 打开，每个请求现开现关；**服务没有任何写接口**：`HEAD` /
  `OPTIONS` / `TRACE` / `POST` / `PUT` / `DELETE` / `PATCH` 一律 405，其中
  `OPTIONS` 等带 `{"error": "method not allowed"}` 体，`HEAD` 按 HTTP 语义只给
  状态码与头、**响应体为空**；未知路径 404 + `{"error": "not found"}`。
- 只绑本机回环地址、无鉴权、无多用户：**设计上不对公网暴露**，不要用反向代理
  或 `--bind 0.0.0.0` 把它放到外网。

500 只说类别，细节只在服务端：

| 类别 | 含义 |
| --- | --- |
| `source_unavailable` | 看板数据源不可用（不存在 / 打不开 / 不是合法库 / 读表失败） |
| `data_invalid` | 卡上的时间戳超出 `datetime.fromtimestamp` 可表示范围（如 13 位毫秒） |
| `internal_error` | payload 构建或序列化失败（如字段是 BLOB、`id` 为 NULL） |

响应体固定为 `{"error": "<类别>: <一句话原因>"}`，**不含绝对路径、表名、SQLite
原文，也不含异常原文**；异常类型与原文（带请求方法与路径）写一行 stderr：

```
[team-board] 500 source_unavailable GET /api/board <- BoardError: 不是合法的 SQLite 库 /…/b.db: file is not a database
```

退出码：

- `0` —— 正常跑（Ctrl-C / SIGINT 收尾也是 0）。
- `1` —— 库不可读（不存在 / 打不开）、端口被占用或无权监听；stderr 以 `错误: `
  开头，如 `错误: 数据源不存在: …`、`错误: 无法监听 127.0.0.1:8787: …`。
- `2` —— 参数错误（`--port` 越界/非整数、`--bind` 非本机地址、未知 flag），
  由 argparse 报错。

已知限制：`/api/summary` 的「完成」按**服务进程本地时区**的自然日计数（与
`summary` 子命令一致）；耗时与超时判断用进程真实时钟，因此用固定 `--now` 建的
fixture 库隔久了再打开，越界卡会变多 —— 这是真实时钟的本意，不是 bug。

卡上用于耗时口径的两个时间戳（起点 `started_at`，缺省回退 `created_at`；终点
`completed_at`）必须是 `datetime.fromtimestamp` 能表示的 epoch 秒：13 位毫秒
这类脏值在 Web 两个接口上一律 500 `data_invalid`（**不再**静默算成 0 分钟）；
命令行子命令不做这个校验，耗时仍按「口径」段的规则夹到下限 0。

## 分支稽核（branch-audit）

一条命令查两类偏差：**卡分支上的提交归属**与 **checkout 偏移**。只读 git，不改任何
分支、不碰工作区。

```
team-board branch-audit [--repo PATH] [--main-branch NAME] [--branch NAME] [--allow-file PATH]
```

- `--repo PATH` —— 要稽核的 git 仓库（默认 `.`）。
- `--main-branch NAME` —— 主分支名（默认 `main`）；本地没有就退回 `origin/<NAME>`。
- `--branch NAME` —— 只查这一条分支（`wt/t_xxx` 或 `t_xxx` 两种写法都行）；
  CI 用它让每张 PR 只对自己的分支负责。指定的分支存在但不是卡分支 → 输出
  `… 不是卡分支，跳过提交归属检查`，只做 checkout 检查。指定的卡号解析不到
  `wt/t_xxx` 卡分支（例如只有漏写 `wt/` 前缀的裸分支）→ **退出码 2**，不会
  静默稽核零条分支后报绿。
- `--allow-file PATH` —— 豁免表（默认 `<repo>/.team-board/branch-audit-allow.txt`）。

**规则 A：卡分支上的提交归属。** 卡分支指名字精确等于 `wt/t_<8 位卡号>` 的本地
分支与 `origin/` 远端分支（`main`、`pm/ci-check`、`wt/ci-setup` 一律跳过）。对
`main..<卡分支>` 的每个非合并提交，申报卡号 = 提交标题里的卡号 ∪ 改动文件路径里的
卡号（**不看正文**）。两种判红：集合为空 → `未声明卡号（无法归属）`；集合里有别的
卡号 → `跨卡提交`，两张卡号都点名。豁免只能按「分支卡号 + 对方卡号（或 `-`）+
提交 sha7..40 + 理由」逐条登记，命中时输出里回显理由，不会静默吞掉。

豁免表格式（`#` 开头为注释；文件不存在视为空表；少字段 / 缺理由 / sha 不是 7–40 位
小写十六进制 → 退出码 2）：

```
# <分支卡号> <对方卡号或 -> <提交sha7..40> <理由，必填>
# 例：t_b5fde972 t_1b200562 52a2f93  W1 的返工卡 t_1b200562 修原卡 P2，同一卡族
```

**规则 B：checkout 偏移。** 主 checkout（`git rev-parse --git-common-dir` 的父目录）
必须停在 `<main-branch>`；其它 worktree 的 detached HEAD 一律判红，分支是
`wt/t_<X>` 时目录名必须精确是 `t_<X>`，其它分支也必须是 `main`。豁免不适用于规则 B。

退出码：

- `0` —— 无偏差（打印人类可读结论，结尾 `结论：无偏差`）。
- `1` —— 发现偏差（结尾 `结论：发现 N 处偏差（提交归属 M 处 / checkout K 处 / 分支结构 J 处）`）。
- `2` —— 参数或环境错误：仓库路径不存在 / 不是 git 工作树、主分支不存在、
  `--branch` 指定的 ref 不存在（含裸卡号解析不到 `wt/t_xxx`）、豁免表某行格式
  非法、豁免表不是 UTF-8 文本、`PATH` 里没有 `git`、**git 输出不是 UTF-8**
  （`i18n.logOutputEncoding=GBK` 或 `core.quotePath=false` + 非 UTF-8 文件名）、
  stdout/stderr 编码表示不了中文；stderr 以 `错误: ` 开头、stdout 为空、不冒
  traceback（argparse 级坏参数照旧 rc2）。崩溃绝不会和「发现偏差」同码。

故意造偏差的演示（全部在 `mktemp -d` 里造仓，跑完自动清理，真仓库状态前后一致）：

```
bash scripts/demo-branch-audit.sh
```

脚本默认拿主 checkout `/home/liu/project/team-board` 做「演示前后未被污染」的断言；
本机没有这个 checkout（容器 / CI）时用 `TB_REAL_REPO=<别的 checkout>` 指定断言对象，
都没有就**明确打 skip 提示**而不是假装断言通过。

## 合并认账稽核（merge-audit）

事后稽核 main 上的**合并提交**有没有写认账三字段 —— 谁按了合并（`Merge-By`）+
依据哪张卡（`Merge-Card`）+ 合并提交/PR 号（`Merge-PR`）。只读 git，不改分支、
不碰工作区。三字段写在合并提交 message **末尾的 trailer 块**（每行一条）：

```
Merge-By: <profile>/run<NNN>       # 例 pm/run137
Merge-Card: t_[0-9a-f]{8}          # 例 t_ad5e7ff6
Merge-PR: #<正整数>                 # 例 #1
```

```
team-board merge-audit [--repo PATH] [--baseline REV] [--baseline-file PATH]
                       [--rev REV] [--card CARD] [--require]
```

- `--repo PATH` —— 要稽核的 git 仓库（默认 `.`）。
- `--baseline REV` / `--baseline-file PATH` —— 基线。**判定**的是
  `<基线>..<rev>` 区间内的合并提交；基线及更早的合并只回显不判定。两者只能给
  一个（同给 → rc2）。都不给时读默认文件 `<repo>/.team-board/merge-audit-baseline`，
  **该文件不存在 → rc2（不静默当成「没有基线」）**；内容须是一行 7–40 位小写
  十六进制 sha（空文件 / 多行 / 非 UTF-8 / 不是 sha 一律 rc2）。
- `--rev REV` —— 区间终点（默认 `HEAD`）；解析不成提交（含空串）→ rc2。
- `--card CARD` —— 只列这张卡的合并记录（**同时过滤判定段与历史段**）；形态不是
  `t_` + 8 位小写十六进制（空串、大写、多字符、缺前缀）→ rc2。它只过滤记录
  清单，**不隐藏已判出的问题**。
- `--require` —— 要求 `--card` 指定的卡有认账合并，**必须与 `--card` 同用**
  （单给 → rc2）：区间内有该卡零问题的合并 → 通过；只有基线之前的合并命中 →
  rc1 `缺认账字段（历史，基线前）`；两段都没命中 → rc1 `缺认账字段`。

区间内的每条合并提交都走 trailer 解析（缺字段 / 格式非法 / 重复字段 / 字段未落在
trailer 区），任一条有问题即 rc1。

**历史段口径**：基线之前的合并只回显 PR 号 / 卡号 / sha，`Merge-By` **无法判定**
（那些提交里没有该字段，也不做历史回填）—— 输出固定写 `by=无法判定`；GitHub
默认标题认不出卡号的（例如 setup PR 的 `wt/ci-setup`）也保留，卡号位置写 `-`，
不静默丢条目。

退出码：

- `0` —— 无问题（`--require` 时该卡有零问题的认账合并）。
- `1` —— 有认账问题（缺字段 / 格式非法 / 重复字段 / 字段未落在 trailer 区 /
  `--require` 未满足）。
- `2` —— 参数或环境错误：仓库路径不存在或不是 git 工作树、基线文件缺失或非法、
  基线或 `--rev` 解析不成提交、卡号形态非法、`--require` 没带 `--card`、`PATH`
  里没有 `git`、git 输出不是 UTF-8、stdout/stderr 编码表示不了中文；stderr 以
  `错误: ` 开头、stdout 为空、不冒 traceback（argparse 级坏参数照旧 rc2）。

### 合并前：`merge-record`（生成 body / 预检 body）

- **生成**：`team-board merge-record --card t_xxx --by pm/runNNN --pr NN --body-out /tmp/t_xxx-merge-body.txt`
  —— 打印认账 body 段（三行 trailer）+ 卡面认账块说明 + `gh pr merge` 命令模板
  （**只打印，不执行任何 git/gh 命令**）；`--body-out` 把 body 段原样写盘（UTF-8，
  末尾恰好一个换行），文件内容与 stdout 的 body 段逐字节一致。未给 `--body-out` 时
  注释里报默认路径 `/tmp/<卡号>-merge-body.txt` 且**不写文件**。
- **预检**：`team-board merge-record --check-file /tmp/t_xxx-merge-body.txt` —— 合并
  **前**校验一份 body 文件的三字段（可选 `--card/--by/--pr` 做一致性校验）；通过
  rc0（`通过：三字段齐（Merge-By/Merge-Card/Merge-PR）`），有问题 rc1（每处一行
  `问题: <kind>：<detail>`），文件/参数级错误 rc2。

三字段的分层口径（机器可判 / 卡面留痕 / 做不到）、合并操作五步与残余风险见
[`docs/merge-accountability.md`](docs/merge-accountability.md)。

## 通用参数

`list` / `summary` / `member` / `export` 四个子命令共享下面两个参数（`serve` /
`branch-audit` / `merge-audit` / `merge-record` 各有自己的参数）：

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

`branch-audit` 沿用同一组数字但语义不同：`1` 表示**发现偏差**（不是数据源错误），
`2` 表示参数或环境错误；详见上面「分支稽核」一节。`merge-audit` 同理：`1` 表示
**有认账问题**、`2` 表示参数或环境错误；详见上面「合并认账稽核」一节。

## 口径

- **耗时**：从开始到结束的分钟数，向下取整。开始取 `started_at`，缺省回退
  `created_at`；结束取 `completed_at`，未结束则算到 `--now`；结束早于开始的
  脏数据夹到 0。时间戳**超出可表示范围**（13 位毫秒等）在 Web 两个接口上会被
  拒成 500 `data_invalid`（见「已知限制」），命令行不做这个校验。
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

### 本地跑静态检查

装开发依赖（版本锁在 `pyproject.toml` 的 `dev` extras 里）：`pip install -e ".[dev]"`。

```
python -m ruff check src tests
python -m mypy
python -m bandit -r src -q --severity-level all --confidence-level all
PYTHONPATH=src TZ=Asia/Shanghai python -m pytest -q
```

前三条分别是风格/未使用残留、类型、明显安全写法；CI 上同名三个 job（`ruff (3.12)` /
`mypy (3.12)` / `bandit (3.12)`）会拦下这三类问题，pytest job 拦测试退化。

跑真实库看效果：

```
.venv/bin/team-board list
.venv/bin/team-board summary
.venv/bin/team-board member engineer
.venv/bin/team-board export
```
