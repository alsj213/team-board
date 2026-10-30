# gate 日志契约：命名、目录、摘要与退出码

`gate` 是**包装器**，不是第二个检查器：它把 `pytest` / `ruff` / `mypy` / 各 `*-audit`
原样跑起来，全量输出写进文件，stdout 只回一段摘要。检查器自己的输出格式一个字不改，
判定值（过没过、几个失败）也不由 gate 重算。

## 1. 命令签名

```
python -m team_board.cli gate --gate <名> --card t_xxxxxxxx [--log-dir DIR] -- <cmd> [args...]
```

| 参数 | 必填 | 含义 |
| --- | --- | --- |
| `--gate <名>` | 是 | 闸门名，日志文件名前缀。字母数字开头，其后字母数字与 `._-` |
| `--card t_xxxxxxxx` | 是 | 卡号，日志文件名后缀。`t_` + 8 位小写十六进制 |
| `--log-dir DIR` | 否 | 日志目录，默认 `evidence` |
| `--` 之后 | 是 | 要执行的命令与参数，按列表 exec、**不经 shell** |

`--` 之后为空 → rc2（`错误: 缺少要执行的命令（-- 之后至少一个）`）。

### 三个可复制样例（pytest / ruff / mypy）

把 `t_facc5c45` 换成自己的卡号即可原样执行。下面是 2026-10-31 CST 的本机实跑原文，
摘要逐字照贴（`log:` 指向跑这条命令的那个 checkout 的 `evidence/`）。

通过的 `pytest`：

```
$ PYTHONPATH=src .venv/bin/python -m team_board.cli gate --gate pytest --card t_facc5c45 -- .venv/bin/python -m pytest tests/unit/test_gatelog.py -q
gate: pytest
card: t_facc5c45
exit: 0
counts: passed=49 failed=0 error=0 skipped=0
失败行（0/0，≤20）:
末尾行（2/2，≤20）:
  .................................................                        [100%]
  49 passed in 0.07s
log: /home/liu/project/tb-wt/t_eaa81611/evidence/pytest-t_facc5c45.log
```

通过的 `ruff`（没有 pytest 风格计数行 → `counts: 未解析`，其余照常）：

```
$ PYTHONPATH=src .venv/bin/python -m team_board.cli gate --gate ruff --card t_facc5c45 -- .venv/bin/python -m ruff check src tests
gate: ruff
card: t_facc5c45
exit: 0
counts: 未解析
失败行（0/0，≤20）:
末尾行（1/1，≤20）:
  All checks passed!
log: /home/liu/project/tb-wt/t_eaa81611/evidence/ruff-t_facc5c45.log
```

通过的 `mypy`：

```
$ PYTHONPATH=src .venv/bin/python -m team_board.cli gate --gate mypy --card t_facc5c45 -- .venv/bin/python -m mypy src
gate: mypy
card: t_facc5c45
exit: 0
counts: 未解析
失败行（0/0，≤20）:
末尾行（1/1，≤20）:
  Success: no issues found in 16 source files
log: /home/liu/project/tb-wt/t_eaa81611/evidence/mypy-t_facc5c45.log
```

## 2. 日志文件：命名、目录、创建

- **命名**：`<gate>-<card>.log`，两个参数都是文件名的组成部分。
- **默认目录**：`evidence`，**相对当前工作目录**（不是相对仓库根，也不是相对卡 worktree），
  不存在就连父目录一起创建。
- **覆盖**：`--log-dir DIR` 换目录，创建规则同上；给了它就不再碰 `evidence/`。
- **名字形态在落盘之前校验**（唯一判据 `team_board.gatelog.log_filename`）：`--gate a/b`、
  空串、`--card t_XYZ` 一律 rc2 且**不产生文件** —— 路径分隔符必须在拼路径之前被拦住，
  否则一次 `--gate a/b` 就会在日志目录下造出子目录。
- **落盘内容**：子命令的 stdout 与 stderr **合并**（`stderr=subprocess.STDOUT`）按写入
  顺序原样写入，不截断、不改写。
- **命令起不来时不写日志**：没有输出可落，日志目录也不会被创建。

## 3. 退出码

| 码 | 含义 |
| --- | --- |
| 子命令的 rc | **原样透传**：0 = 通过，1 = 有失败/有告警，其它值照传 |
| `128 + n` | 子命令被信号 `n` 杀掉（`SIGTERM` → 143），摘要与日志里不带负数 |
| `2` | 参数或环境错误：`--gate` / `--card` 形态非法、`--` 之后没有命令、命令起不来（找不到可执行文件 / 没权限 / 不是目录）、日志写不进去、stdout 编码表示不了中文 |

`2` 上压着两层语义，**透传优先**：被包装的命令自己返回 2 时 gate 也返回 2，两者同码不冲突，
靠 stderr 区分 —— 工具自己的错误恒是**恰一行** `错误: …` 且 stdout 为空；透传的情形
stdout 有完整摘要、stderr 为空。

本机实跑（同一 worktree，2026-10-31 CST）：

```
$ ... gate --gate rc2-sub --card t_facc5c45 --log-dir <tmp>/logs -- /bin/sh -c 'echo hi; exit 2'
gate: rc2-sub
card: t_facc5c45
exit: 2
counts: 未解析
失败行（0/0，≤20）:
末尾行（1/1，≤20）:
  hi
log: <tmp>/logs/rc2-sub-t_facc5c45.log          ← 透传：有摘要、stderr 空
rc=2

$ ... gate --gate ghost --card t_facc5c45 -- nosuchcmd_xyz --version
错误: 启动失败：nosuchcmd_xyz: [Errno 2] No such file or directory: 'nosuchcmd_xyz'
rc=2                                            ← 工具自己的错误：stderr 恰一行、stdout 空、无日志

$ ... gate --gate ghost --card t_XYZ -- true
错误: 卡号形态非法: t_XYZ
rc=2
```

## 4. 输出约定

**stdout 只有摘要，stderr 只在出错时一行 `错误: …`。** 摘要字段顺序恒为：

```
gate: <名>
card: <卡号>
exit: <码>
counts: passed=<n> failed=<n> error=<n> skipped=<n>   ← 解析不到任何计数时是「未解析」
失败行（<已列>/<命中总数>，≤20）:
  <失败行…>
末尾行（<已列>/<非空行总数，≤20）:                   ← 只在「失败行」零命中且有非空行时出现
  <末尾行…>
log: <日志文件的绝对路径>
```

- 「失败行」= 含 `FAILED` / `ERROR` / `error:` / `错误:` / `Traceback` 或行首 `E   ` 的行
  （去重保序，最多 `gatelog.FAIL_LINE_LIMIT` = 20 条）；括号里是「已列/命中总数」，截断
  在摘要里可见（命中 25 条就是 `20/25`）。
- 命中为零时「失败行」写 `0/0`，退回末尾若干行的单列进「末尾行」一节 —— 通过的 `ruff`
  就是这一形态（`All checks passed!`）。
- `log:` 恒指向一个**已经存在**的文件：实现先落盘、后打印摘要，所以「日志没写成」只会
  是 rc2，不会留下半截 stdout。

### 用法：卡上只贴摘要 + 日志路径

一张卡的验收只需要**判定值**，不需要检查器的逐行输出。落到交付物上的规范是：

1. 卡面 / 评论 / `summary` 只贴**摘要原文 + `log:` 的绝对路径**，不贴检查器原文；
2. 要更多行就贴路径 + `sed -n '<a>,<b>p' <日志>` 取段，而不是把全文搬进上下文；
3. 摘要里的 `exit:` / `counts:` 就是可直接复述给验收方的判定值（R3 要的退出码 + 命令原文）；
4. 单条评论 / `tasks.result` 的 UTF-8 字节上限 `gatelog.OUTPUT_LIMIT_BYTES` = 8192，
   全文贴法一超就违规（`output-audit` 判 rc1）。

## 5. 边界

- **不改变既有子命令的输出契约**：`body-audit` / `branch-audit` / `merge-audit` 等一行没动，
  gate 只是把它们包起来跑；判定与格式仍归它们自己。
- **接入旧工具的方式 = 包一层 gate**，不是改造工具本身：工具照旧裸跑可用，要落盘 + 摘要
  就在它前面加 `gate --gate <名> --card <卡号> --`。
- **gate 不判定通过与否**：`exit:` 是被包装命令的 rc 透传，`counts:` 是从它自己的输出里
  解析的值；gate 不重算、不解释、不改写。
- 本文只描述契约，改动实现在 `src/team_board/cli.py`（`gate` 子命令）与
  `src/team_board/gatelog.py`（命名 / 落盘 / 摘要的唯一实现）。

## 6. 本机实跑记录

2026-10-31 01:17–01:20 CST，worktree `/home/liu/project/tb-wt/t_eaa81611`（分支
`wt/t_eaa81611`，含 `gate` 子命令），Python `.venv/bin/python`（3.14.7）：

| 场景 | 命令里的 `--gate` | rc |
| --- | --- | --- |
| 通过 | `pytest tests/unit/test_gatelog.py -q` | 0 |
| 通过 | `ruff check src tests` | 0 |
| 通过 | `mypy src` | 0 |
| 失败透传 | `pytest` 一个必失败用例 | 1（`counts: passed=1 failed=1`） |
| 透传 2 | `/bin/sh -c 'echo hi; exit 2'` | 2（有摘要、无 stderr） |
| 被信号杀 | `/bin/sh -c 'kill -9 $$'` | 137（`128 + 9`，摘要里不带负数） |
| 起不来 | `nosuchcmd_xyz` | 2（stderr 恰一行、无日志） |
| 卡号非法 | `--card t_XYZ` | 2（stderr 恰一行、无日志） |
| `--` 后为空 | `--` | 2（stderr 恰一行） |
| `--log-dir` 覆盖 | `--log-dir <tmp>/logs` | 0，日志落在覆盖目录、`evidence/` 未新建 |
