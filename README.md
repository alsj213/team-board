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

共十个子命令：`list` / `summary` / `member` / `export` / `branch-audit` / `merge-audit` /
`merge-record` / `commit-audit` / `body-audit` / `serve`（`serve` 见下面「Web 看板」一节，
`branch-audit` 见「分支稽核」一节，`merge-audit` / `merge-record` 见「合并认账稽核」一节，
`commit-audit` 见「提交身份稽核」一节）。

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

一条命令查三类偏差：**卡分支上的提交归属**、**checkout 偏移** 与 **分支结构
（一卡一分支）**。只读 git，不改任何分支、不碰工作区。

```
team-board branch-audit [--repo PATH] [--main-branch NAME] [--branch NAME] [--allow-file PATH]
```

- `--repo PATH` —— 要稽核的 git 仓库（默认 `.`）。
- `--main-branch NAME` —— 主分支名（默认 `main`）；本地没有就退回 `origin/<NAME>`。
- `--branch NAME` —— 只查这一条分支（`wt/t_xxx` 或 `t_xxx` 两种写法都行）；
  CI 用它让每张 PR 只对自己的分支负责。它**只影响规则 A 与 C-2**：**C-1 永远全仓
  枚举、与 `--branch` 无关**（所以 CI 里只传 `--branch` 也照样拦得住「一卡两分支」）。
  指定的分支存在但不是卡分支 → 输出 `… 不是卡分支，跳过提交归属检查`，规则 A 与
  C-2 都不跑（C-1 照跑）。指定的卡号解析不到 `wt/t_xxx` 卡分支（例如只有漏写
  `wt/` 前缀的裸分支）→ **退出码 2**，不会静默稽核零条分支后报绿。
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

**规则 C：一卡一分支。** 两类判红，**豁免表对规则 C 同样不生效**（豁免只对规则 A
生效，现 0 条）：

- **C-1「一卡多分支」**：卡分支只能叫 `wt/<卡号>`。分支名里申报了卡号、但分支名
  **不精确等于** `wt/<卡号>`（`wt/t_xxxxxxxx-fix`、裸 `t_xxxxxxxx`、`x/t_xxxxxxxx`、
  `wt/t_xxxxxxxx/y` 都算）→ 判红；同一分支名的本地与 `origin/` 两份**合并成一条**
  （不会因两边都在而重复判红）。分支名里提取不到卡号的（`main`、`pm/ci-check`、
  `wt/ci-setup`）一律跳过；`evidence/<单段名>`（R11 证据分支命名空间）不参与 C-1。
  **C-1 是全仓枚举、与 `--branch` 无关** —— 因此 CI 只传 `--branch` 也照样拦。
- **C-2「fork 点不在 main」**：取卡分支上**第一个申报本卡号**的提交，其父提交
  （fork 点）必须落在本地 `<main-branch>` 或 `origin/<main-branch>` 任一上（本地
  `main` 可能落后 `origin/main`，只看本地会造假红）；没有父提交（孤立历史）判红；
  fork 点落在别的卡分支上则点名对方卡号（`（它属于卡 t_xxxxxxxx 的分支）`）。
  C-2 受 `--branch` 影响。
- 报告新增段 `分支结构偏差（N 处）`；结论行 `… （提交归属 a 处 / checkout b 处 /
  分支结构 c 处）`，`分支结构 c 处` 无条件打印（0 也打）。

**爆炸半径**（务必先读再动分支）：C-1 是全仓枚举 —— 任何**一条**含合法卡号却不是
`wt/<卡号>` 的分支（`wt/t_x-fix`、裸 `t_x`、`x/t_x`、`evidence2/t_x`、rebase 备份
分支，或本地未 `git fetch origin --prune` 留下的 stale `origin/*` 残留）都会让
**所有** PR 的 `branch-audit` job 一起变红；而规则 C 不适用豁免表（豁免只对规则 A
生效，现 0 条），撞上只能删/改分支。本地跑之前先 `git fetch origin --prune`。

退出码：

- `0` —— 无偏差（打印人类可读结论，结尾 `结论：无偏差`）。
- `1` —— 发现偏差（结尾 `结论：发现 N 处偏差（提交归属 M 处 / checkout K 处 / 分支结构 J 处）`）。
- `2` —— 参数或环境错误：仓库路径不存在 / 不是 git 工作树、主分支不存在、
  `--branch` 指定的 ref 不存在（含裸卡号解析不到 `wt/t_xxx`）、豁免表某行格式
  非法、豁免表不是 UTF-8 文本、`PATH` 里没有 `git`、**git 输出不是 UTF-8**
  （`i18n.logOutputEncoding=GBK` 或 `core.quotePath=false` + 非 UTF-8 文件名）、
  stdout/stderr 编码表示不了中文；stderr 以 `错误: ` 开头、stdout 为空、不冒
  traceback（argparse 级坏参数照旧 rc2）。崩溃绝不会和「发现偏差」同码。

故意造偏差的演示（全部在 `mktemp -d` 里造仓，跑完自动清理，真仓库状态前后一致）。
共 6 个场景，逐条比对退出码与关键文案，任一不符脚本自己 rc1：

- 场景 A —— 卡分支上出现别的卡号的提交（跨卡提交）；
- 场景 A' —— 卡分支上的提交没写卡号（未声明卡号）；
- 场景 B —— 主 checkout 停在卡分支 + 卡 worktree 目录名不对（checkout 偏移）；
- 场景 C —— 同一卡号有第二条分支 `wt/t_xxxxxxxx-fix`（规则 C-1 一卡两分支）；
- 场景 D —— 从 `wt/t_bbbbbbbb` 拉出 `wt/t_aaaaaaaa`（规则 C-2 fork 点不在 main）；
- 绿色基线 —— main 干净 + 卡分支干净 + 卡 worktree 目录名 = 卡号。

```
bash scripts/demo-branch-audit.sh
```

脚本默认拿主 checkout `/home/liu/project/team-board` 做「演示前后未被污染」的断言；
本机没有这个 checkout（容器 / CI）时用 `TB_REAL_REPO=<别的 checkout>` 指定断言对象，
都没有就**明确打 skip 提示**而不是假装断言通过。

## 合并认账稽核（merge-audit）

事后稽核 main 上的**合并提交**有没有写认账三字段 —— 谁按了合并（`merged-by`）+
依据哪张卡（`merge-card`）+ 合并提交/PR 号（`merge-pr`）。只读 git，不改分支、
不碰工作区。三字段写在合并提交 message **末尾的 trailer 块**（每行一条，键名
大小写敏感、**恒小写**；旧的 `Merge-By` / `Merge-Card` / `Merge-PR` 一律不再
接受，也不做大小写兼容别名）：

```
merged-by: <profile>/run<NNN>       # 例 pm/run137
merge-card: t_[0-9a-f]{8}          # 例 t_ad5e7ff6
merge-pr: #<正整数>                 # 例 #1
```

```
team-board merge-audit [--repo PATH] [--baseline REV] [--baseline-file PATH]
                       [--rev REV] [--card CARD] [--db PATH] [--require]
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
  清单，**不隐藏已判出的问题**。给了它就**同时跑判据①**（见下）。
- `--db PATH` —— 判据① 读 `tasks.assignee` 用的看板库，**只读**
  （`mode=ro`，不建表、不写），默认
  `/home/liu/.hermes/kanban/boards/devteam/kanban.db`。只在给了 `--card` 时用。
- `--require` —— 要求 `--card` 指定的卡有认账合并，**必须与 `--card` 同用**
  （单给 → rc2）：区间内有该卡零问题的合并 → 通过；只有基线之前的合并命中 →
  rc1 `缺认账字段（历史，基线前）`；两段都没命中 → rc1 `缺认账字段`。

区间内的每条合并提交都走 trailer 解析（缺字段 / 格式非法 / 重复字段 / 字段未落在
trailer 区），任一条有问题即 rc1。

**判据①（`merged-by` == 该卡 assignee）**：给了 `--card` 时，`merged-by` 值里
`/` 之前的 profile 必须**严格等于**该卡在看板库里的 `tasks.assignee`（只读
`--db` 指定库的 `SELECT assignee FROM tasks WHERE id = ?`）。不符 → 一条问题
`merged-by 与卡 assignee 不符`（`detail` 写 `merged-by=<原值> assignee=<卡 assignee>`）
→ rc1，输出里多一行证据（格式固定）：

```
判据①（merged-by == 卡 assignee）：卡 t_ad5e7ff6 assignee=pm，merged-by=pm/run137 → PASS
```

不符时该行写 `→ FAIL`；没给 `--card` 时整段不打印。历史段（基线之前）的合并
没有 `merged-by` 值，**不参与**判据①，照样只回显 `by=无法判定`。库不存在 / 不是
合法 SQLite / 没有 `tasks` 表 / 卡不存在 / 卡没有 assignee 一律 rc2（**不静默
跳过判据**）。

**历史段口径**：基线之前的合并只回显 PR 号 / 卡号 / sha，`merged-by` **无法判定**
（那些提交里没有该字段，也不做历史回填）—— 输出固定写 `by=无法判定`；GitHub
默认标题认不出卡号的（例如 setup PR 的 `wt/ci-setup`）也保留，卡号位置写 `-`，
不静默丢条目。

退出码：

- `0` —— 无问题（`--require` 时该卡有零问题的认账合并）。
- `1` —— 有认账问题（缺字段 / 格式非法 / 重复字段 / 字段未落在 trailer 区 /
  `merged-by 与卡 assignee 不符` / `--require` 未满足）。
- `2` —— 参数或环境错误：仓库路径不存在或不是 git 工作树、基线文件缺失或非法、
  基线或 `--rev` 解析不成提交、卡号形态非法、`--require` 没带 `--card`、看板库
  不可读或不是合法 SQLite、库里没有 `--card` 指定的卡、`PATH` 里没有 `git`、
  git 输出不是 UTF-8、stdout/stderr 编码表示不了中文；stderr 以
  `错误: ` 开头、stdout 为空、不冒 traceback（argparse 级坏参数照旧 rc2）。

### 合并前：`merge-record`（生成 body / 预检 body）

- **生成**：`team-board merge-record --card t_xxx --by pm/runNNN --pr NN --branch wt/t_xxx --body-out /tmp/t_xxx-merge-body.txt`
  —— 打印认账 body 段（三行 trailer）+ 卡面认账块说明 + **一行 `merge_guard` 合并命令**
  （R12 ① 的唯一入口，带齐 `--method merge` / `--delete-branch` / 认账四参数；
  **只打印，不执行任何 git/gh 命令**）；`--body-out` 把 body 段原样写盘（UTF-8，
  末尾恰好一个换行），文件内容与 stdout 的 body 段逐字节一致，命令里的 `--body-file`
  恒为**绝对路径**。生成模式下 `--branch` 与 `--body-out` **必填**（缺任一项 → rc2、
  stdout 空、stderr 一行 `错误: …`），`--branch` 形态非法（空串 / 含空白 / 带 `refs/`
  前缀）同样是 rc2。`--repo`（默认 `.`）只用来拼命令里的 `cd` / `PYTHONPATH` /
  `--repo` 三处。打印行里 `--branch` 与这三处**都过 `shlex.quote`**：仓库路径含空格、
  分支名含 `;` / `$` / `|`（`git check-ref-format` 认的合法名）时照抄仍是**一条**命令；
  `--subject` 含换行 → rc2（否则打印行会被切成半条命令）。
- **预检**：`team-board merge-record --check-file /tmp/t_xxx-merge-body.txt` —— 合并
  **前**校验一份 body 文件的三字段（可选 `--card/--by/--pr` 做一致性校验）；通过
  rc0（`通过：三字段齐（merged-by/merge-card/merge-pr）`），有问题 rc1（每处一行
  `问题: <kind>：<detail>`），文件/参数级错误 rc2。

三字段的分层口径（机器可判 / 卡面留痕 / 做不到）、合并操作五步与残余风险见
[`docs/merge-accountability.md`](docs/merge-accountability.md)。

## 合并后删源分支（merge_guard）

合并闸门：**合并命令必须带 `--delete-branch`**，合并**前**必须确认源分支在远端真的存在，
合并**后**必须机器确认远端源分支真的消失了 —— 「先存在、后消失」两半都验，才构成
「被删」这个结论。
入口和出口都靠退出码说话 —— 本工具**不提供不带删除的合并路径**：没有
`--no-delete-branch` / `--keep-branch` 这种开关，也没有 `--admin` / `--auto` / `--force`
这类绕过 PR 检查的口子。它是独立模块，不是 `team-board` 的子命令：

```
python3 -m team_board.merge_guard \
  --pr <正整数> --branch <PR 的 head 分支名> \
  [--method {merge}=merge] --delete-branch \
  --card <t_ 加 8 位小写十六进制> --by <profile>/run<NNN> \
  --subject <合并提交标题> --body-file <认账 body 文件> \
  [--repo PATH=. ] [--remote NAME=origin] [--timeout SEC=60] [--interval SEC=2] \
  [--command-timeout SEC=60] [--verify-only]
```

- `--pr` / `--branch` / `--delete-branch` 三项**必填**。`--method` **只认 `merge`**，默认值就是
  `merge`（可省略）；传 `squash` / `rebase` 一律 **rc2**、stdout 空、stderr 一行、**一个 gh
  调用都不发** —— 合并方式统一定一种（create a merge commit，R13）。`--delete-branch` 缺了就
  直接 rc2，且**一个 gh 写操作都不发**，
  stderr 固定一行
  `错误: 合并命令缺少 --delete-branch（合并卡必须带；本工具不提供不带删除的合并路径）`。
- **认账四参数（合并模式必填）**：`--card`（形如 `t_1234abcd`）、`--by`（形如 `pm/run200`）、
  `--subject`（单行非空白合并提交标题）、`--body-file`（含 `merged-by` / `merge-card` /
  `merge-pr` 三行 trailer 的文件，由 `team-board merge-record --body-out` 生成）—— 四者
  **原样转发**给 `gh pr merge`。缺任一项 / 形态非法 / `--subject` 含换行 / body 文件不存在、
  不是普通文件、非严格 UTF-8、为空 / body 内容经 `merges.check_message` 判出问题
  （缺字段、重复、格式非法、与 `--card` / `--by` / `--pr` 不一致）→ **rc2、stdout 空、
  stderr 恰一行、零 `gh` 零 `git` 调用**（全部在 `ensure_branch_present()` 之前判完）。
  `--body-file` **必须是绝对路径**：本工具按进程 cwd 校验 body，而 `gh pr merge` 的 cwd 是
  `--repo`（`--repo` 默认 `.`），同一个相对路径在两处指向不同文件 —— 轻则 gh 报读不到
  （rc1 冒充「未达成」），重则静默合进另一份 body、合出**没有 trailer** 的提交。相对路径
  → rc2（2026-10-29 P2-1 返工）。
  为什么必填：`gh pr merge` 不带 `--subject` / `--body-file` 时用 gh 默认文案生成合并提交
  message，**没有任何 trailer**，经本工具合出来的提交必然过不了 `merge-audit --require`
  （缺认账三字段）。`--verify-only` 与这四个参数**互斥**（同给 → rc2）。
- `--pr` 取值 1–999999999（13 位毫秒这种误用一律 rc2）；`--timeout` / `--interval` /
  `--command-timeout` 必须是 ≥1 的 ASCII 十进制整数（`0`、负数、`1_000`、空串、空串之外的
  空白都 rc2），且**各有 3600 秒的上界**（`MAX_TIMEOUT` / `MAX_INTERVAL` /
  `MAX_COMMAND_TIMEOUT` 三个模块常量），另有硬约束 `--interval ≤ --timeout`。
  上界不是装饰：没有它时 `clock() + timeout` 会裸抛
  `OverflowError: int too large to convert to float`，而那时**合并已经发生了**。
- `--command-timeout`（默认 60）是**单次子命令**的硬超时：预检的 `gh pr view` 超时 → rc2；
  轮询里的 `git ls-remote` 超时 → 记一次通信错误继续轮询（绝不当成「分支还在」或「已删」）。
- `--repo` 默认 `.`，gh 与 git 都以它为工作目录（gh 由 git remote 自己认仓库）；
  `--remote` 默认 `origin`。
- `--branch` 等于该 PR 的 `baseRefName`，或等于 `main` / `master` → rc2（防删主分支）。
- **合并前先确认源分支在远端存在**：`git -C <repo> ls-remote --exit-code --refs <remote>
  refs/heads/<branch>` 的 `rc=0` 才算存在，其它任何 rc（`2` = 没有这个 ref、`128` =
  通信错误…）一律 **rc2**、stdout 空、stderr 一行、**一个 gh 调用都不发**（这一步排在
  `gh pr view` 之前）。没有这一步，「这个 ref 从来没存在过」会被出口判定读成「已删除」：
  PR 来自 fork（gh 不删 fork 侧分支，探针在本仓 origin 上必然 rc2）或 `--remote` 指到
  另一个没有该分支的 remote 时，这条误判必现。
- 合并前预检 `gh pr view <pr> --json
  number,state,headRefName,baseRefName,headRefOid`：`state` 必须是 `OPEN`、
  `headRefName` 必须与 `--branch` **逐字相等**，`headRefOid` 必须是 40 位（SHA-1）或
  64 位（SHA-256）十六进制提交 SHA —— 缺字段或格式不对一律 rc2，说清实际值。预检通过
  后把提交钉进证据：`head sha | PASS | <sha>`。
- 合并只用 `gh pr merge <pr> --<method> --delete-branch --match-head-commit <sha>
  --subject <subject> --body-file <path>`
  （`<sha>` = 预检读到的 `headRefOid`；后两个参数来自 `--subject` / `--body-file`，顺序固定）。
  只比分支名不约束内容：预检到合并之间源分支被
  force-push / 追加提交时，合的会是预检**从未见过**的提交，工具却照样打 PASS + rc0。
  带上 `--match-head-commit` 后由 gh 自己比对，head 变了直接拒。老版本 gh 报
  `unknown flag: --match-head-commit` → **rc2**、stdout 空、stderr 一行，**不许**静默
  降级成「不带 sha 合并」。**gh 返回非 0 不等于合并失败**：
  2.103.3 的 `mergeRun` 顺序是 `merge()` → `deleteLocalBranch()` → `deleteRemoteBranch()`，
  删本地分支失败就返回非 0（`failed to delete local branch …`），而那时 PR 已合、远端
  分支还没删。所以非 0 之后必须 `gh pr view <pr> --json state` 反查：
  - `state=MERGED` → **不许**写「合并失败」；gh 的退出码与输出原文留证，然后照常做删除
    验证（分支已删 → rc0；分支仍在 → rc1；判不了 → 「合并已完成但无法判定」rc2）；
  - `state` 不是 `MERGED`（含反查本身失败）→ 维持 rc1「合并失败」，且不跑删除验证。
- 合并后的唯一判据是轮询
  `git -C <repo> ls-remote --exit-code --refs <remote> refs/heads/<branch>`：`rc=0` =
  分支仍在远端、`rc=2` = 没有这个 ref（已删除**或从未存在过**，单看这一步分不出来）、
  其它 rc = 判不了（记一次继续轮询）。轮询间隔 `--interval`，总等待不超过 `--timeout`：
  `--timeout` 是**总**预算 —— 每次探针的超时 = `min(--command-timeout, 剩余预算)`，
  剩余预算 ≤ 0 时不再起子进程，直接按「无法判定」收尾（合并路径 rc2、`--verify-only`
  rc1），所以一次卡住的 `git` 不会把总耗时顶成 `--timeout + --command-timeout`。
- `--verify-only`：不合并，只回答「远端分支**现在**在不在」，**一次 gh 都不调**
  （巡检 / 取红样例用）。它**不能**证明「曾经存在并被删」：远端没有这个 ref 时结论是
  `结论：远端没有这个 ref（已删除，或从未存在过）`，rc 仍是 0 —— 要证明「被删」，
  只能走合并路径（先有「分支存在」预检，再有「分支删除」验证）。它的步骤行单独一种
  形态 `远端 ref | <存在|不存在|无法判定> | <命令原文> | rc=<n> | <关键输出>`，
  **不写** `PASS` / `FAIL`、也不出现「分支删除」—— 这条路径一次删除都没验证过，
  打 PASS 会被抄进卡当「删除已验证」的证据（2026-10-29 P2-1 返工）。它与认账四参数
  （`--card` / `--by` / `--subject` / `--body-file`）**互斥**：同给 → rc2（verify-only
  不合并，给 body 说明调用方误判）。

退出码：

- `0` —— 合并完成且已确认删除（`--verify-only` 时为「远端没有这个 ref」，**不等于**
  「曾经存在并被删」）。
- `1` —— 未达成：合并失败（gh 非 0 且 PR 状态不是 `MERGED`）、或合并成功但分支仍在；
  `--verify-only` 时还包括「分支仍在」与「无法判定」（`结论：无法判定（…）`）。合并路径上
  「判不了」**不是** rc1 而是 rc2（见下条）。
- `2` —— 参数或环境错误，含缺 `--delete-branch`、`--method` 不是 `merge`（`squash` /
  `rebase`）、`--branch` 形态非法（空串 / 含空白字符 / 带 `refs/` 前缀；合并路径与
  `--verify-only` **同一套**校验，都在一切 `gh` / `git` 调用之前）。也包含：合并模式缺
  `--card` / `--by` / `--subject` / `--body-file` 任一项、`--card` / `--by` 形态非法、
  `--subject` 空 / 纯空白 / 含换行、`--body-file` 路径为空 / **不是绝对路径**（相对路径会在
  进程 cwd 与 `--repo` 两处解析成不同文件，见上）/ 不存在 / 不是普通文件 /
  非严格 UTF-8 / 空文件、body 内容不合认账三字段（缺字段 / 重复 / 格式非法 / 与参数
  不一致）、`--verify-only` 与这四个认账参数同给、`--pr` / `--timeout` /
  `--interval` / `--command-timeout` 越界、`--interval > --timeout`、子命令超时、
  **合并前源分支在远端不存在**、**合并前 rebase 闸门拒绝**（`baseRefName` 形态非法 /
  `git fetch` 取远端提交失败 / 远端 head 与预检的 `headRefOid` 不一致 / `base` 不是
  `head` 的祖先 / 祖先关系判不了，见「合并前：rebase 闸门」）、`--branch` 是主分支或与 PR
  的 `headRefName` 不一致、
  `headRefOid` 缺失或不是 40/64 位十六进制、gh 不支持 `--match-head-commit`、
  PR 不是 `OPEN`、`PATH` 里没有 `gh` / `git`、gh/git 输出不是 UTF-8、stdout 编码表示
  不了中文、收到 `SIGTERM` / `SIGINT` / `SIGHUP`（转成「无法判定」收尾），以及
  **合并已发生但通信持续失败导致判不了**（这一种不算 1：判不了就是未通过，
  `rc2` 一律 stderr 一行 `错误: …`、不冒 traceback，**退出码绝不为 1**）。

**rc2 不保证没发起合并**：合并成功后探针判不了也是 rc2（stderr 会写明合并已完成、请人工
核对）—— 见到 rc2 先看 stdout 有没有已经打出来的步骤行，别直接把它读成「没合并、可以重跑」。

stdout 只有一处例外：**合并已经发生之后**才出错时（合并后的探针超时、`git` 不可用、
意外异常、判不了），stdout 仍打印已完成的「分支存在」「预检」「合并」几步 —— 后面的
崩溃不许把「合并成功了」这条证据一起吞掉，否则下一轮会读成「没合并、可以重跑」。此时
stderr 仍是一行 `错误: …`、退出码仍是 2。

**写不出去不改退出码**：stdout 被提前关掉是常态（`… | head -0`、`1>&-`）。管道断开
（`BrokenPipeError` / `EPIPE`）、流已关闭、`sys.stdout is None` 三种形态一律**不冒
traceback**，把 fd 1 重定向到 `/dev/null` 后按本次运行的**真实结论**返回 —— 本来 rc1
仍是 1、本来 rc0 仍是 0、本来 rc2 仍是 2，不会变成 120 / 141（`120` 是「退出时 flush
stdout 失败」，`141` 是 SIGPIPE）。

**收到信号也要留住证据**：`main()` 期间 `SIGTERM` / `SIGINT` / `SIGHUP` 一律转成「无法
判定」收尾 —— 已完成的步骤行照常写 stdout，stderr 一行
`错误: 收到信号 SIGTERM（退出码不是 1；合并是否已发生请人工核对）`，退出码 `2`（不是
124 / 130 / 143）。合并已发生却被杀掉时若留下 0 字节 stdout，下一轮就会把它读成
「没合并、可以重跑」。

输出每步一行：`<步骤> | <PASS|FAIL> | <命令原文> | rc=<n> | <关键输出一行>`，步骤标签固定
`分支存在` / `预检` / `合并` / `分支删除`（按执行顺序；预检后另有一行 `head sha | PASS |
<sha>` 记下钉住的提交，再往后是合并前的 `rebase | PASS | …` 闸门行，见下一小节），末行固定
`结论：` 前缀。下面这段是假 gh / 假 git 跑出来的端到端样例（真仓库只有路径与 sha 那段不同）：

```
分支存在 | PASS | git -C <repo> ls-remote --exit-code --refs origin refs/heads/wt/t_bbeab29f | rc=0 | c5768d4	refs/heads/wt/t_bbeab29f
预检 | PASS | gh pr view 9 --json number,state,headRefName,baseRefName,headRefOid | rc=0 | state=OPEN, headRefName=wt/t_bbeab29f, baseRefName=main
head sha | PASS | 4f2a9c0e5b1d3a7c8e6f90b2d4a1c3e5f7b9d0a2
rebase | PASS | git -C <repo> merge-base --is-ancestor refs/merge-guard/9/base refs/merge-guard/9/head | rc=0 | b3c1d2e 是 head 4f2a9c0 的祖先（已 rebase 到 main）
合并 | PASS | gh pr merge 9 --merge --delete-branch --match-head-commit 4f2a9c0e5b1d3a7c8e6f90b2d4a1c3e5f7b9d0a2 --subject Merge PR #9（t_bbeab29f） --body-file /tmp/t_bbeab29f-merge-body.txt | rc=0 | (无输出)
分支删除 | PASS | git -C <repo> ls-remote --exit-code --refs origin refs/heads/wt/t_bbeab29f | rc=2 | 分支已不在远端
结论：合并完成，源分支 wt/t_bbeab29f 已从 origin 删除
```

### 合并前：rebase 闸门

`merge_guard` 在**发起合并之前**还会用真实远端提交判定「PR 的 base（`origin/main`）是不是 head
的祖先」—— 不是就 **rc2 拒绝合并、一个 `gh pr merge` 都不发**；已 rebase 才放行。判据三条命令
（base 取 `gh pr view` 读到的 `baseRefName`，不写死 `main`；私有 ref 落在
`refs/merge-guard/<pr>/{base,head}`）：

```
git -C <repo> fetch --no-tags --no-recurse-submodules --quiet <remote> \
    '+refs/heads/<base>:refs/merge-guard/<pr>/base' \
    '+refs/heads/<branch>:refs/merge-guard/<pr>/head'
git -C <repo> rev-parse refs/merge-guard/<pr>/head      # 必须逐字等于预检的 headRefOid
git -C <repo> merge-base --is-ancestor refs/merge-guard/<pr>/base refs/merge-guard/<pr>/head
```

分支落后 main 时（本机 2026-10-29 真跑：临时 bare origin + clone + 假 gh，分支 `wt/t_deadbeef`
从 `main~1` 拉出；命令同上那行，只把 `--pr` / `--branch` / `--repo` / `--card` / `--by` /
`--subject` / `--body-file` 换成 9 / wt/t_deadbeef / 克隆目录 / t_deadbeef / engineer/run232 /
`Merge PR #9（t_deadbeef）` / body 文件；返工卡 `t_0a16dc4d` 用同一脚本重跑，下面两段是新代码
的原文）：

```
# rc=2；stdout 0 字节；stderr 恰一行：
错误: 分支 wt/t_deadbeef 落后 main（main 不是 head 的祖先，git merge-base --is-ancestor rc=1）：合并前必须先 rebase 到 main（在作者的 bot 身份 worktree 里做：bash scripts/setup-bot-identity.sh <profile> <worktree>；或改用 git merge origin/main 把 main 并入分支 —— merge 提交被 git log --no-merges 排除，不触 commit-identity 判据③），拒绝合并
# $FAKE_LOG 里 gh 只被调过 1 次（那次 gh pr view）；gh pr merge 次数=0
```

`git rebase origin/main` 之后同一条命令 rc0，stdout 依次为（真跑原文，路径已换成 `<repo>`）：

```
分支存在 | PASS | git -C <repo> ls-remote --exit-code --refs origin refs/heads/wt/t_deadbeef | rc=0 | bea0a7373d4c5e6d80340785735749113327ed27	refs/heads/wt/t_deadbeef
预检 | PASS | gh pr view 9 --json number,state,headRefName,baseRefName,headRefOid | rc=0 | state=OPEN, headRefName=wt/t_deadbeef, baseRefName=main
head sha | PASS | bea0a7373d4c5e6d80340785735749113327ed27
rebase | PASS | git -C <repo> merge-base --is-ancestor refs/merge-guard/9/base refs/merge-guard/9/head | rc=0 | 9f017ba 是 head bea0a73 的祖先（已 rebase 到 main）
合并 | PASS | gh pr merge 9 --merge --delete-branch --match-head-commit bea0a7373d4c5e6d80340785735749113327ed27 --subject Merge PR #9（t_deadbeef） --body-file /tmp/rg-e2e-QJ07sd/body.txt | rc=0 | (无输出)
分支删除 | PASS | git -C <repo> ls-remote --exit-code --refs origin refs/heads/wt/t_deadbeef | rc=2 | 分支已不在远端
结论：合并完成，源分支 wt/t_deadbeef 已从 origin 删除
```

- 拒绝的四种原因**全部归 rc2**（「合并前状态不满足」，与「源分支远端不存在」「PR 不是 OPEN」同
  档）：取远端提交失败（`git fetch` 非 0）/ 远端 head 与预检的 `headRefOid` 不一致（预检之后被
  推送）/ `base` 不是 head 的祖先（落后或分叉）/ 祖先关系判不了（`merge-base` 非 0/1）。
  畸形 `baseRefName`（空串 / 含空白 / 带 `refs/` 前缀）同样 rc2，且**闸门自身不发任何 git
  调用**（`fetch` 之前就拦；此前已跑过一次 `ls-remote` 存在性探针，那是合并前的源分支存在
  闸门，不是本闸门发的）。
- **谁来做 rebase**：闸门只判「已 rebase 与否」，不做也不替你选执行者。要么在**这批提交作者的
  bot 身份 worktree 里**做（先 `bash scripts/setup-bot-identity.sh <profile> <worktree>`，否则
  committer 变成执行者、`commit-audit` 判「committer 与 author 不一致」）；要么改用
  `git merge origin/main` 把 main 并入分支 —— merge 提交被 `git log --no-merges` 排除，不触
  `commit-identity` 的 committer≡author 判据（判据③）。同一句短版也印在拒绝文案里。
- **不加跳过开关**：没有 `--allow-stale` / `--skip-rebase-check` / `--force` / `--no-verify`。
  `--verify-only` 路径也不加这一步（它一次 gh 都不调）。
- 它**不取代** R6 的「合并前 CI 全绿」，也不改 R12/R13 的判据：合并入口仍然只有 `merge_guard`
  一条命令，这只是它内部多一道闸门。条文与残余风险见 `docs/merge-rebase-gate.md`。

合并前的源分支不存在时（rc2，stdout 空，一个 gh 调用都没发）。合并模式必填认账四参数，
`--body-file` 指的 body 先由 `team-board merge-record --card <卡号> --by <profile>/run<NNN> --pr 9 --branch wt/<卡号> --body-out <body文件>` 生成
（body 文件缺失 / 缺 trailer / 与 `--card`·`--by`·`--pr` 不一致同样 rc2，见上一节）：

```
$ python3 -m team_board.merge_guard --pr 9 --branch wt/t_never_existed_zzz --method merge --delete-branch \
    --card <卡号> --by <profile>/run<NNN> --subject <subject> --body-file <body文件>
# stderr: 错误: 源分支 wt/t_never_existed_zzz 不在 origin 远端（rc=2）：(无输出)；合并前源分支必须先存在，拒绝合并
```

`--branch` 形态非法时同样 rc2、stdout 空、零 `gh` / 零 `git`（合并路径与 `--verify-only`
同一套校验，三种输入各一行错误）：

```
$ python3 -m team_board.merge_guard --pr 9 --branch '' --method merge --delete-branch --verify-only
# stderr: 错误: --branch 不能是空串：要的是裸分支名（例 wt/t_1234abcd）；空串会拼成 refs/heads/ 这个不存在的 ref，ls-remote 的 rc=2 会被读成「已删除」

$ python3 -m team_board.merge_guard --pr 9 --branch ' wt/t_x' --method merge --delete-branch --verify-only
# stderr: 错误: --branch ' wt/t_x' 不能含空白字符（第 1 个字符是 ' '）：要的是裸分支名，空白不会被 Git 规范化掉，refs/heads/<含空白> 在远端永远查不到

$ python3 -m team_board.merge_guard --pr 9 --branch 'refs/heads/wt/t_x' --method merge --delete-branch --verify-only
# stderr: 错误: --branch 'refs/heads/wt/t_x' 不能带 refs/ 前缀：要的是裸分支名（例 wt/t_1234abcd），本工具自己拼 refs/heads/；重复前缀会查成 refs/heads/refs/… 而永远不存在
```

`--verify-only` 的输出（一次 gh 都不调；注意步骤行是 `远端 ref`，不写 `PASS`）：

```
远端 ref | 不存在 | git -C <repo> ls-remote --exit-code --refs origin refs/heads/wt/t_bbeab29f | rc=2 | 分支已不在远端
结论：远端没有这个 ref（已删除，或从未存在过）
```

分支还在时最后两行长这样（rc1，**不算合并成功**）：

```
分支删除 | FAIL | git -C <repo> ls-remote --exit-code --refs origin refs/heads/wt/t_bbeab29f | rc=0 | c5768d4	refs/heads/wt/t_bbeab29f
结论：合并成功但源分支仍在远端（合并不算完成）
```

## 提交身份稽核（commit-audit）

事后稽核卡分支上**每条提交的提交者身份**（判据②③）：author 必须是本卡允许的执行者，
committer 必须是本 bot 且与 author 是同一个身份。只读 git 与看板库（`mode=ro`），
不改分支、不碰工作区。

判据①②③ 的完整口径、边界（页面级身份区分做不到）与「每 bot 提交身份怎么配」也写在
[`docs/merge-accountability.md`](docs/merge-accountability.md)。

```
team-board commit-audit --card t_[0-9a-f]{8} [--repo PATH] [--rev REV]
                        [--main-branch NAME] [--db PATH] [--bots PATH]
```

- `--card CARD` —— **必填**。判据② 的白名单要读该卡**建卡时**的 `assignee`（看板库
  `task_events` 里 `kind='created'` 事件的 payload，只读），卡号形态不是 `t_` + 8 位
  小写十六进制（空串、大写、缺前缀、裸 8 位）→ rc2；看板库里没有这张卡的建卡事件 /
  payload 里没有非空 `assignee` → rc2（不静默放宽白名单，也不回退 `tasks.assignee`）。
- `--repo PATH` —— 要稽核的 git 仓库（默认 `.`）；不是工作树 → rc2。
- `--rev REV` —— 区间终点（默认 `HEAD`）；解析不成提交（含空串）→ rc2。
- `--main-branch NAME` —— 区间起点取 `origin/<NAME>`，**不存在则退回
  `refs/heads/<NAME>`**（本地没 fetch 过也能跑）；两者都没有 → rc2。
- `--db PATH` —— 看板库，默认 `/home/liu/.hermes/kanban/boards/devteam/kanban.db`。
- `--bots PATH` —— bot 身份表，默认 `<repo>/.team-board/bots.tsv`。

判定口径（只看区间内的**非合并提交**，`git log --no-merges`）：

- **判据② author 白名单**：`author` 邮箱的 localpart（@ 之前那段）必须落在
  `{该卡**建卡时** assignee, engineer, qa}` 里，否则问题 `author 不在白名单`。
  建卡时 assignee 取自看板库 `task_events` 里 `kind='created'` 事件的
  `payload.assignee` —— **不用 `tasks.assignee`**：`kanban_request_review` 会把卡
  改派给 reviewer，拿它当白名单会让同一批提交「验收前判红、验收后判绿」。
- **判据③ committer 是本 bot**：`committer` 邮箱的 localpart 必须登记在
  `bots.tsv` 的 profile 集合里（否则 `committer 不是本 bot`），且必须与 author
  的 localpart 相同（否则 `committer 与 author 不一致`，两条可同时出现）。
- **身份表里没有该身份**：author 的 localpart 不在 `bots.tsv` 里 —— 这条提交的作者
  没有登记过的 bot 身份，单独点名，免得只看到「committer 不是本 bot」而以为作者没问题。
- 区间为空 → 输出一行 `区间内无提交`、rc0。

为什么判定权在 `commit-audit` + CI 而不在钩子：`--no-verify`、提交消息里先写
`Co-authored-by`、`core.hooksPath` 被重装，三种都能让钩子失效；钩子只做早警。

退出码：

- `0` —— 无问题（区间为空也算）。
- `1` —— 有问题（上面四类问题任一）。
- `2` —— 参数或环境错误（卡号形态非法、仓库不是工作树、身份表缺失 / 不是 UTF-8 /
  某行不是三列 / profile 重复、看板库不可读或卡不存在、`--rev` 解析不成提交、
  找不到主分支、`PATH` 里没有 `git`、git 输出不是 UTF-8、stdout 编码表示不了中文）；
  stderr 以 `错误: ` 开头、stdout 为空、不冒 traceback（argparse 级坏参数照旧 rc2）。

### 每 bot 提交身份怎么配

身份表 `.team-board/bots.tsv` 是唯一身份源（`profile<TAB>name<TAB>email`，`#` 开头
的注释行与空行忽略）：

```
pm	pm	pm@team-board.local
engineer	engineer	engineer@team-board.local
...
```

注意 GitHub 页面**恒显示 `alsj213`**（单账号），这张表只解决「提交元数据里能区分」，
不解决页面显示 —— 别指望它让网页出现六个头像。

每个 bot 在自己的 worktree 里跑一次（幂等，重复跑不报错）：

```
bash scripts/setup-bot-identity.sh engineer /home/liu/project/tb-wt/<卡号>
```

脚本做四件事：从 `<worktree>/.team-board/bots.tsv` 查 profile（查不到 → stderr
提示 + exit 2）、给主仓开 `extensions.worktreeConfig true`、写本 worktree 的
`--worktree user.name/user.email`（只影响这个 worktree，不动主仓、不动别的卡）、
打印写入后的 `user.email`。

之后每次 `git commit` 都会被 `.githooks/pre-commit` 早警：`user.email` 的 localpart
不在表里、或 `git var GIT_COMMITTER_IDENT` 里的邮箱与它不一致（`git -c user.email=…
commit` 就是这么暴露的）→ 打印 `[pre-commit] committer 不是本 bot：<实际>；先跑
bash scripts/setup-bot-identity.sh <profile> <worktree>` 并 exit 1。钩子能被执行
`--no-verify` / 重装 `core.hooksPath` 绕过，所以它只是早警，判定以 `commit-audit`
与 CI job `commit-identity` 为准。

## 通用参数

`list` / `summary` / `member` / `export` 四个子命令共享下面两个参数（`serve` /
`branch-audit` / `merge-audit` / `merge-record` / `commit-audit` 各有自己的参数）：

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
`commit-audit` 同理：`1` 表示**有身份问题**、`2` 表示参数或环境错误；详见上面
「提交身份稽核」一节。

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
