# 闸门日志（gate）：检查器输出落盘，上下文只收判定值

`gate` 是**包装器**，不是第二个检查器：它把 `pytest` / `ruff` / `mypy` / 各 `*-audit`
原样跑起来，全量输出写进文件，stdout 只回一段摘要。落盘与摘要的唯一实现是
`src/team_board/gatelog.py`（`write_log` / `render_summary`），`cli.py` 只负责「去
`--`」「跑子进程」「先落盘再打印摘要」，不复制任何阈值或格式。

## 用法

```
$ PYTHONPATH=src python3 -m team_board.cli gate --gate <名> --card t_xxxxxxxx [--log-dir DIR] -- <cmd> [args...]
```

- `--gate` / `--card` 必填，且都是日志文件名的组成部分：`--gate` 只许字母数字开头、
  其后字母数字与 `._-`；`--card` 只许 `t_` + 8 位小写十六进制。形态非法（空串、含 `/`、
  `t_XYZ`）在**落盘之前**就拒 —— 否则 `--gate a/b` 会拼出子目录。
- `--log-dir` 默认 `evidence`（**相对当前目录**，不存在就建）。
- `--` 之后是要执行的命令及其参数。argparse 会把 `--` 本身留在 `REMAINDER` 里，wrapper
  负责去掉；去掉后为空 → rc2。
- 子命令的 stdout 与 stderr **合并**（`stderr=subprocess.STDOUT`）按写入顺序落盘，
  日志不截断。

## 退出码

| 码 | 含义 |
| --- | --- |
| 子命令的 rc | **原样透传**（`pytest` 有失败 → 1，`ruff` 有告警 → 1，…） |
| `128 + n` | 子命令被信号 `n` 杀掉（`SIGTERM` → 143），摘要里不带负数 |
| `2` | 参数或环境错误：`--gate` / `--card` 形态非法、`--` 之后没有命令、命令起不来（`FileNotFoundError` / `PermissionError` / `NotADirectoryError`）、日志写不进去、stdout 编码表示不了中文 |

`2` 上面压着两层语义：工具自己的「参数或环境错误」与被包装命令恰好返回 2。两者同码
不冲突 —— 透传优先，看 stderr 区分（工具自己的错误恒是**恰一行** `错误: …`，且
stdout 空）。

命令没起来时**不写日志**（没有输出可落），日志目录也不会被创建。

## 摘要格式

字段顺序恒为：

```
gate: <名>
card: <卡号>
exit: <码>
counts: passed=<n> failed=<n> error=<n> skipped=<n>    ← 解析不到任何计数时是「未解析」
失败行（<已列>/<命中总数>，≤20）:
  <失败行…>
末尾行（<已列>/<非空行总数>，≤20）:                    ← 只在「失败行」零命中时出现
  <末尾行…>
log: <日志文件的绝对路径>
```

- 「失败行」= 含 `FAILED` / `ERROR` / `error:` / `错误:` / `Traceback` 或行首 `E   `
  的行（去重保序，最多 20 条）；括号里是「已列数/命中总数」，截断在摘要里可见（25 条
  命中就是 `20/25`）。
- 命中为零时「失败行」写 `0/0`，退回**末尾多少行**的单列在「末尾行」一节 —— 通过的
  `ruff` 就是这一形态（`All checks passed!`）。两节分开是为了不让「非失败行」挂在
  「失败行」标题下。
- 逐字格式、去重与截断规则的唯一实现是 `team_board.gatelog.render_summary`：本文不抄
  逐字样例，避免两处会漂。

## 三个真跑样例

下面三段是 2026-10-30 22:12 CST 在本机实跑的原文（`--log-dir /tmp/g`，输出逐字照贴）。
看三件事：stdout 只有摘要、`counts:`/`exit:` 是判定值、`log:` 指向真实文件。

通过的 `pytest`（`失败行` 零命中 → 尾巴行进「末尾行」节）：

```
$ PYTHONPATH=src .venv/bin/python -m team_board.cli gate --gate pytest --card t_eaa81611 --log-dir /tmp/g -- .venv/bin/python -m pytest tests/integration/test_cli_gate.py -q
gate: pytest
card: t_eaa81611
exit: 0
counts: passed=13 failed=0 error=0 skipped=0
失败行（0/0，≤20）:
末尾行（2/2，≤20）:
  .............                                                            [100%]
  13 passed in 0.39s
log: /tmp/g/pytest-t_eaa81611.log
rc=0
```

通过的 `ruff`（没有 pytest 风格的计数行 → `counts: 未解析`，其余照常）：

```
$ PYTHONPATH=src .venv/bin/python -m team_board.cli gate --gate ruff --card t_eaa81611 --log-dir /tmp/g -- .venv/bin/python -m ruff check src tests
gate: ruff
card: t_eaa81611
exit: 0
counts: 未解析
失败行（0/0，≤20）:
末尾行（1/1，≤20）:
  All checks passed!
log: /tmp/g/ruff-t_eaa81611.log
rc=0
```

`body-audit`（18 行报告整份进日志，stdout 只留摘要；为省篇幅这里只贴「末尾行」节的前 2 行，
完整 18 行在日志里）：

```
$ PYTHONPATH=src .venv/bin/python -m team_board.cli gate --gate body-audit --card t_eaa81611 --log-dir /tmp/g -- .venv/bin/python -m team_board.cli body-audit
gate: body-audit
card: t_eaa81611
exit: 0
counts: 未解析
失败行（0/0，≤20）:
末尾行（18/18，≤20）:
  /home/liu/.hermes/kanban/boards/devteam/kanban.db
  口径：body 为 UTF-8 字节数；NULL body 记 NULL；上限 2048 字节
  …（后 16 行见 /tmp/g/body-audit-t_eaa81611.log）
log: /tmp/g/body-audit-t_eaa81611.log
rc=0
```

看板数据随时变，`body-audit` 的明细行数会跟着变（本样例是 18 行）；**命令与摘要口径不变**。

## 为什么

一张 QA 卡单次上下文 15.6 万 tok 里，**130 KB 是 terminal 原始输出**（pytest 的进度行、
ruff 的逐文件列表）。判定其实只有几个值：过没过、几个失败、失败在哪几行。全文进上下文
等于把日志搬进模型窗口，既贵又不准（关键失败行被淹没）。

全量落盘之后，上下文只收摘要，要看原文再按需取 —— 日志文件在磁盘上是完整的，摘要是
可复跑命令的输出。

## 与 R3 的关系

R3 要求「每条 PASS 对应一条本机实跑命令 + 真实退出码」，本子命令就是为此设计的：

- 卡上贴**摘要 + 日志绝对路径**，不贴原文；
- 失败行最多 20 行（`gatelog.FAIL_LINE_LIMIT`），要更多就贴路径 + `sed -n '<a>,<b>p' <日志>` 取段；
- `counts:` 与 `exit:` 是可直接复述给验收方的判定值，`log:` 指向的证据文件在
  `kanban_attach` 之后仍可下载。

反过来看：单条评论 / `tasks.result` 的 UTF-8 字节上限是
`gatelog.OUTPUT_LIMIT_BYTES`（8192），这个阈值也是同一个模块给出的唯一来源。
