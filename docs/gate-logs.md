# gate 日志契约：命名、目录、摘要与退出码

`gate` 是**包装器**，不是第二个检查器：它把 `pytest` / `ruff` / `mypy` / 各 `*-audit`
原样跑起来，全量输出写进文件，stdout 只回一段摘要。检查器自己的输出格式一个字不改，
判定值（过没过、几个失败）也不由 gate 重算。

## 0. 为什么要有 gate

需求卡 `t_023f2827` 的契约给的原话：一张 QA 卡单次上下文 15.6 万 tok 里，130KB 是 terminal
输出；全量落盘后上下文只收判定值。gate 就是把这句落地的包装器 —— 它不判定检查器过没过，
只负责「全量进文件、摘要进上下文」。本节的数字出自该契约，**不是**本文档自己测的。

## 1. 命令签名

```
python -m team_board.cli gate --gate <名> --card t_xxxxxxxx [--log-dir DIR] -- <cmd> [args...]
```

| 参数 | 必填 | 含义 |
| --- | --- | --- |
| `--gate <名>` | 是 | 闸门名，日志文件名前缀。字母数字开头，其后字母数字与 `._-` |
| `--card t_xxxxxxxx` | 是 | 卡号，日志文件名后缀。`t_` + 8 位小写十六进制 |
| `--log-dir DIR` | 否 | 日志目录，默认 `evidence` |
| `--` 之后 | 否（**一律带上**） | 要执行的命令与参数，按列表 exec、**不经 shell** |

`--` 之后为空 → rc2（`错误: 缺少要执行的命令（-- 之后至少一个）`）。

`--` 严格说可省，但**一律带上**：首个 token 不像选项时 parser 直接把它当位置参数 —— 实测
`gate --gate nosep --card t_facc5c45 --log-dir /tmp/gl472 /bin/echo hi` → 摘要齐全、rc0；
首个 token **像选项**（`-q`、`--foo`）时会被**顶层** parser 吞掉，落的是顶层文案而不是
`team-board gate:` —— 实测 `… --log-dir /tmp/gl472 -q true` → rc2、stderr **3 行**（本卡树 253
字节；主 checkout @ `21d40b9` 少了 `output-audit` 子命令 → 240 字节 —— 字节数随 usage 那一行的
子命令列表变，**别拿它当判据**，判据只有「3 行 + 首末行原文」），
首行 `usage: team-board [-h]`、末行 `team-board: error: unrecognized arguments: -q`、
stdout 0 字节、无日志（不落在 §3 那条「两行 `team-board gate: error:`」的例外里）。

### 前置：`gate` 只在含 PR #50 的树里；解释器写绝对路径；cwd 是本卡 worktree

四条前置，缺一条样例都跑不通：

1. **`gate` 子命令只存在于含 PR #50 的树**（2026-10-31 CST 合入 `main`，merge `21d40b9`）。
   **不是「任何 checkout 都能跑」**：2026-10-31 在引入 `gatelog.py` 的那个提交之前（`00c523e^`，
   `git archive 00c523e^ | tar -x -C <dir>` 解出的树）实测
   `team-board: error: argument command: invalid choice: 'gate'`、rc2、无日志。自查：外层用
   `PYTHONPATH=<树>/src <绝对解释器> -m team_board.cli gate --help`，rc=0 才继续；rc≠0 就把外层的
   `PYTHONPATH` 换成**含 gate 的树的绝对路径**（下面样例就是这么写的）。
   **卡本身改 `gate` / `gatelog` / `cli` 时，外层 `PYTHONPATH` 必须指本卡树**（`<本卡 worktree>/src`
   的绝对路径），否则 gate 自己那份实现是从 `main` 的树 import 的，摘要由旧实现渲染 —— 本卡
   diff 一行没验。实测同一 worktree、只换外层 `PYTHONPATH` 跑同一个探针（`import team_board,
   team_board.gatelog` 后打 `__file__`）：外层 `…/team-board/src` →
   `…/team-board/src/team_board/gatelog.py`；外层 `…/tb-wt/t_facc5c45/src` →
   `…/tb-wt/t_facc5c45/src/team_board/gatelog.py`。
2. **解释器写绝对路径**。`.venv` 在 checkout 根目录**不保证存在**：`t_eaa81611` 根目录就没有
   （个别 worktree 有，2026-10-31 当日 29 个里 6 个），所以下面外层跑 `gate` 与 `--` 之后跑检查器，
   都用 `/home/liu/project/team-board/.venv/bin/python`。照抄 `.venv/bin/python` 在没有 `.venv`
   的 checkout 里会得到 `No such file or directory`、rc=127（2026-10-31 实测）。
3. **cwd = 本卡 worktree**，不是主 checkout。日志本身合法不代表验的是本卡：在主 checkout 里
   「把卡号换成自己的」跑出来的是主 checkout 的代码，本卡 diff 一行没验 —— 那是假证据。同理
   `--` 之后的检查器要用 `/usr/bin/env PYTHONPATH=<本卡 worktree>/src` 把 import 拉回本卡
   worktree，否则外层 `PYTHONPATH` 的绝对路径会一路带下去，pytest 跑的仍是那棵树（同一 cwd
   实测 `team_board.__file__`：外层 `PYTHONPATH=/home/liu/project/team-board/src` →
   `…/team-board/src/team_board/__init__.py`；加 `env PYTHONPATH=src` →
   `/home/liu/project/tb-wt/t_facc5c45/src/team_board/__init__.py`）。
   `R21` 管的是建卡时的 `workspace_kind`，**不约束运行目录**；但样例 cwd 必须是**你自己那张卡**的
   worktree，借别人的 worktree 跑出来的日志不算本卡证据。
4. **`PYTHONPATH` 一律写绝对路径**。相对 `src` 只在**仓库根**成立 —— 它是按 **cwd** 解析的，
   不是按解释器所在的那棵树解析。cwd 一旦不在仓库根（子目录、`cd` 进 `docs/`、脚本里换了目录），
   相对 `src` 会**静默**退到 `.venv` 里 editable 装的主 checkout：不报错、rc 照样 0。
   实测同一 worktree 跑同一个 `which_tree` 探针（打完 `team_board.__init__` / `team_board.gatelog`
   的 `__file__`）：`cwd=<worktree 根>` + `PYTHONPATH=src` → `…/tb-wt/t_facc5c45/src/team_board/__init__.py`；
   `cwd=<worktree>/docs` + 同一个 `PYTHONPATH=src` → `…/team-board/src/team_board/__init__.py`
   （**主 checkout**）；把值换成 `/home/liu/project/tb-wt/t_facc5c45/src` → 又回到本卡树。
   所以样例统一用 `$PYSRC` 存绝对路径，不带相对的 `src`。

### 三个可复制样例（pytest / ruff / body-audit）

把 `t_facc5c45` 换成自己那张卡的卡号、`cd` 换成本卡的 worktree，**三处一起换**（卡号、`cd`、
`PYSRC`；只换卡号不改 cwd 就是前置第 3 条的假证据）。下面是 2026-10-31 CST 在**本卡 worktree**
`/home/liu/project/tb-wt/t_facc5c45` 里的本机实跑原文，摘要逐字照贴（`log:` 指向该 worktree
的 `evidence/`）。三个样例共用下面四行环境准备（`PYSRC` 写绝对路径，理由见前置第 4 条）：

```
$ cd /home/liu/project/tb-wt/t_facc5c45                  ← 换成你自己卡的 worktree
$ PY=/home/liu/project/team-board/.venv/bin/python
$ PYSRC=/home/liu/project/tb-wt/t_facc5c45/src           ← 本卡树，绝对路径（别写相对的 src）
$ export PYTHONPATH=$PYSRC                               ← 外层：gate 自己也从这棵树 import
```

通过的 `pytest`：

```
$ $PY -m team_board.cli gate --gate pytest --card t_facc5c45 -- /usr/bin/env PYTHONPATH=$PYSRC $PY -m pytest tests/unit/test_gatelog.py -q
gate: pytest
card: t_facc5c45
exit: 0
counts: passed=49 failed=0 error=0 skipped=0
失败行（0/0，≤20）:
末尾行（2/2，≤20）:
  .................................................                        [100%]
  49 passed in 14.03s
log: /home/liu/project/tb-wt/t_facc5c45/evidence/pytest-t_facc5c45.log
```

通过的 `ruff`（没有 pytest 风格计数行 → `counts: 未解析`，其余照常）。**「未解析」只对通过的情形成立**：
失败时 ruff 的 `Found 2 errors.` 正好命中 `_COUNTS_RE`，被解析成
`counts: passed=0 failed=0 error=2 skipped=0`（`failed` 恒 0，数字看着像「没失败」）——
**判定看 `exit:` 不看 `counts:`**（2026-10-31 实测：同一 worktree、两处 F401 的文件 → `exit: 1`、
`counts: … error=2 …`、`末尾行（20/27，≤20）` 里带着 `Found 2 errors.`）：

```
$ $PY -m team_board.cli gate --gate ruff --card t_facc5c45 -- /usr/bin/env PYTHONPATH=$PYSRC $PY -m ruff check src tests
gate: ruff
card: t_facc5c45
exit: 0
counts: 未解析
失败行（0/0，≤20）:
末尾行（1/1，≤20）:
  All checks passed!
log: /home/liu/project/tb-wt/t_facc5c45/evidence/ruff-t_facc5c45.log
```

`body-audit`（真板全量 `--all`；rc=1，见下方注）：

```
$ $PY -m team_board.cli gate --gate body-audit --card t_facc5c45 -- /usr/bin/env PYTHONPATH=$PYSRC $PY -m team_board.cli body-audit --all
gate: body-audit
card: t_facc5c45
exit: 1
counts: 未解析
失败行（0/0，≤20）:
末尾行（20/292，≤20）:
    t_9515987a  done       pm         1067  ok
    t_f51328fe  done       pm         1025  ok
    t_b42125ae  todo       pm         1014  ok
    t_e0866bd4  done       ops        1008  ok
    t_216a3cf7  done       qa          963  ok
    t_a5b444fc  running    qa          959  ok
    t_9abe2b3b  done       pm          951  ok
    t_f0a27fe4  done       pm          944  ok
    t_facc5c45  done       engineer    929  ok
    t_844fc961  done       pm          924  ok
    t_472f2d63  done       engineer    876  ok
    t_341e9163  done       pm          874  ok
    t_5158a9e8  done       pm          861  ok
    t_bd5e3f2b  done       pm          773  ok
    t_efe6f4aa  done       pm          772  ok
    t_c8465af4  done       pm          738  ok
    t_985dbba8  done       pm          726  ok
    t_f487b347  done       pm          336  ok
    t_c3ec3d54  done       pm           90  ok
  超限清单（142 张）：t_f43fa954 t_df640caa t_c836202a t_f3a4fb31 t_41195fb3 t_51f5fad0 t_705687b9 t_4dbf3ae2 t_025de8fe t_069dff27 t_0630a104 t_c9f5866a t_bbeab29f t_3e2a3582 t_991ae40f t_bb6600d8 t_6d5fcf9d t_416057c4 t_5dcafcc7 t_d558ed1e t_b5fde972 t_86f4ab63 t_1e795562 t_ec05d75d t_18713037 t_7ec9e801 t_66c5bac9 t_bbdb794a t_11ef8cf3 t_27daff67 t_86bc3d77 t_f27fbe62 t_12a95eed t_aed80ce3 t_1b200562 t_406be0e8 t_8886ae85 t_631b3f59 t_8f6d5e62 t_9c7f4103 t_927ebb83 t_d2e53461 t_a867da93 t_55009247 t_38509317 t_daba51a7 t_a847ee8f t_68b0a37a t_ecfebe60 t_b78582d0 t_38895c77 t_6b5564aa t_e4656674 t_5d631291 t_e8523c7b t_d22c6399 t_e1cffdca t_7605bfde t_0f09a2b9 t_322e3086 t_8fdc0fbe t_37a51cf3 t_02ad8064 t_cf6bea8e t_8d3291e8 t_0c70c181 t_bd1ced18 t_c07fc14f t_dab6af03 t_668b1909 t_244c0f44 t_1b4d66ff t_2df659ad t_c4302856 t_3e9448f7 t_589cc33a t_c0b674f7 t_3ff92267 t_67cd6c50 t_06c7b458 t_eeefcd01 t_7623a70a t_5154323b t_c6e1f623 t_ce436613 t_c51b7da5 t_476de4a5 t_ad5e7ff6 t_ea915a70 t_c34fbc5e t_8f1ef2f4 t_b9e29152 t_d5149e95 t_35eb0568 t_3700f2a2 t_50013602 t_4b9d456d t_59be4c53 t_90ee3bb7 t_b43ab8a5 t_a8c95560 t_a0ceb572 t_a5c2f894 t_270b9cbf t_4e0e8df4 t_11587ce8 t_58ecad09 t_63785d45 t_2001f0c2 t_dfdbf806 t_ceea0ac2 t_ab3f9b30 t_2e0098fe t_c9787079 t_24015d59 t_439d06bf t_da18937f t_f1e56147 t_e32041c1 t_09100683 t_59c0eae1 t_98ce4cf7 t_9d27934d t_ad16e55a t_c9148b7c t_24e43d1a t_29fbd64c t_d637dca6 t_17550b03 t_40fa5375 t_0b2fd9a3 t_c4ce9108 t_f37aecba t_1b965ab5 t_6a97c910 t_6efb3105 t_6fab0d6e t_90c4f66d t_a65d7674 t_5a465efb t_d804b545 t_946b6472
log: /home/liu/project/tb-wt/t_facc5c45/evidence/body-audit-t_facc5c45.log
```

注：`exit: 1` 来自真板的**历史存量** —— `--all` 把已 `done` 的卡一起算（当日 `合计：278 张；超限：142 张`）；
**不是**本文档 / 本卡造成。同一命令去掉 `--all`（只看未结卡：当日 18 张、超限 0 张）时 rc=0 ——
两者都是 rc 原样透传。上面与这里的数字是 **2026-10-31 CST 当日快照**，真板卡数会漂移，复跑以当场输出为准。
**别整行贴「超限清单」**：它是**单行**、随卡数线性增长（实测 142 张 = 1590 字节 ≈ 11.2 字节/卡），
卡再多几百张就顶到 8192 的单条上限（§4 第 5 条）。卡上只贴到「末尾行」节，或把该行截成
`超限清单（142 张）：t_f43fa954 …` 再给 `log:` 路径。

### 树里没有 `gate` 时：兜底等价写法

PR #50 之前的树，或还没把 `21d40b9` rebase 进来的 worktree，`gate` 是 `invalid choice`（前置第 1 条）。
先换外层 `PYTHONPATH` 到含 gate 的树；实在没有这样的树（例如主 checkout 还没 fast-forward）时，
用等价的纯 shell 写法落盘 —— 全量照旧进文件，只是摘要要自己补：

```
$ cd /home/liu/project/tb-wt/t_xxxxxxxx || { echo '错目录: 不是本卡 worktree' >&2; exit 2; }
$ mkdir -p evidence
$ env PYTHONPATH=/home/liu/project/tb-wt/t_xxxxxxxx/src <命令> > evidence/<名>-<卡号>.log 2>&1; echo rc=$?
```

三段都有用，别省：

- **`cd … || { …; exit 2; }`**：`cd` 失败时用 `;` 接着跑，等于在**错的 cwd** 里跑检查器、日志也写进
  错的目录，`rc=` 照样是 0 —— 假证据。实测 `cd /nonexistent && echo hi` → rc=2（shell 的，不是
  检查器的）；`cd /nonexistent; echo hi > evidence/x.log` → **rc=0**（echo 成功，最危险的那种）。
  `|| { … exit 2; }` 把「目录不对」与「检查器失败」分开：前者固定 2 且先打印错目录。
- **`mkdir -p evidence`**：`evidence/` 不存在时 `> evidence/<名>-<卡号>.log` 的重定向本身失败，
  shell 返回 2、检查器**根本没跑** —— 这个 2 会被当成检查器「有环境错误」。实测目标目录不存在
  → rc=2 且无日志；先 `mkdir -p` → rc=0。
- **`env PYTHONPATH=<本卡 worktree>/src` 这个前缀不是装饰**（写绝对路径，理由见前置第 4 条）：
  `.venv` 里 `team-board` 是**已安装**的、指主 checkout，而前置第 1 条又让你
  `export PYTHONPATH=<含 gate 的树>/src`。少了前缀，pytest 跑的是主 checkout 的代码 —— 本卡
  diff 一行没验，rc 照样是 0，就是前置第 3 条说的假证据。同 cwd 实测 `team_board.__file__`：
  沿用外层 `PYTHONPATH` → `/home/liu/project/team-board/src/team_board/__init__.py`；
  加 `env PYTHONPATH=src` → `/home/liu/project/tb-wt/t_facc5c45/src/team_board/__init__.py`。
  **别用 `unset PYTHONPATH` 代替**：实测 unset 之后 import 到的仍是
  `/home/liu/project/team-board/src/…`（editable 装的就是那棵树），一样验错树。

`$?` 是被包装命令自己的 rc（2026-10-31 实测 `/bin/sh -c 'exit 1'` → `rc=1`，日志里有它的输出）。
代价是**没有摘要**：`counts:` / 失败行 / 末尾行 一律没有，卡上能贴的只剩 `rc=` 和日志绝对路径。

## 2. 日志文件：命名、目录、创建

- **命名**：`<gate>-<card>.log`，两个参数都是文件名的组成部分。
- **默认目录**：`evidence`，**相对当前工作目录**（不是相对仓库根，也不是相对卡 worktree），
  不存在就连父目录一起创建。
- **覆盖语义**：同名 `<gate>-<card>.log` **再跑一次是整份覆盖**，不是追加 —— 实现就是
  `open(path, "w", encoding="utf-8", newline="\n")`（`team_board.gatelog.write_log`），上一轮的内容
  一个字都不留。想留痕就先把手上的摘要/日志另存，或者换个 `--gate` / `--log-dir` 让文件名不同；
  否则「上一轮通过、这一轮失败」只剩最后一次的现场，回看时连有没有过过都看不出来。
- **`--log-dir DIR`**：换目录，创建规则同上；给了它就不再碰 `evidence/`。
- **名字形态在落盘之前校验**（唯一判据 `team_board.gatelog.log_filename`）：`--gate a/b`、
  空串、`--card t_XYZ` 一律 rc2 且**不产生文件** —— 路径分隔符必须在拼路径之前被拦住，
  否则一次 `--gate a/b` 就会在日志目录下造出子目录。
- **落盘内容**：子命令的 stdout 与 stderr **合流**（`stderr=subprocess.STDOUT`），先按**解释器的文本
  编码**解码（`subprocess.run(..., text=True, errors="replace")`），再以 UTF-8 写文件
  （`gatelog.write_log` 里 `encoding="utf-8"`）。三条边界：
  - **默认就是 UTF-8**，但来源有两个，**都不是**「Python 3.7+ 的 UTF-8 mode 默认开」：① locale 本身
    是 UTF-8（本机 `LANG=C.UTF-8`，`sys.flags.utf8_mode == 0`）；② locale 是 `C`/POSIX 时 **PEP 538**
    把 C locale 协同成 `C.UTF-8`，并借 **PEP 540** 打开 UTF-8 mode —— 实测 `LC_ALL=C` 下
    `sys.stdout.encoding == 'utf-8'`、`sys.flags.utf8_mode == 1`。UTF-8 mode **默认开**要到
    **PEP 686 / Python 3.15**（PEP 原文 `Python will enable UTF-8 mode by default from Python 3.15`）；
    本机解释器是 3.14.7，仍靠上面两条。两条任一成立时，检查器输出按原字节还原，不经过任何替换；
  - **显式 `PYTHONUTF8=0` / `-X utf8=0` 且 locale 非 UTF-8**（例 `LC_ALL=C`，实测 `-X utf8=0` 下
    `sys.stdout.encoding == 'ascii'`）时解码用 `ascii`，非 ASCII 字节全变 `U+FFFD` —— 实测
    `printf '中文ok\n'` 落成 `ef bf bd`×6 + `ok`；此时 gate 自己的 stdout 也编不出中文摘要，
    **必然 rc2**（摘要恒含中文，不是「多以」）—— `PYTHONIOENCODING=ascii` 与 `PYTHONUTF8=0 LC_ALL=C`
    两次实测都是 rc2、stderr 恰一行 `错误: 当前 stdout 编码 'ascii' 无法表示输出里的中文…`，而日志
    在退出**之前**已写好（两次都留下 3 字节 `hi\n`）—— 读到 U+FFFD 先看卡上 rc 是不是 2，别当
    检查器的输出坏了；
  - 检查器输出里的**非 UTF-8 字节**同样替换为 `U+FFFD`，不截断。
  跨流的严格时序**不保证** —— 合流只保证同一个 fd 上的先后，子进程自己的
  stdout 缓冲会让两流相对顺序与程序逻辑顺序不同。
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

上面那句有一个例外：**argparse 参数解析失败**（缺必填参数、未知子命令、未知选项）发生在
`_run_gate` 之前，落的是 argparse 自己的文案，不是 `错误:` 那一行 —— stderr 是 `usage: …` 加
`team-board gate: error: …` **两行**（无 `错误:` 前缀），stdout 仍为 0 字节，rc 仍为 2，不写日志。
（若连 `--` 也省了、首个 token 又像选项，报错来自**顶层** parser：`usage: team-board [-h]` 开头、
`team-board: error: unrecognized arguments: …` 结尾的 **3 行**，见 §1 对 `--` 的说明。）

2026-10-31 实测（缺 `--gate`）：

```
$ $PY -m team_board.cli gate --card t_facc5c45 -- true
usage: team-board gate [-h] --gate NAME --card CARD [--log-dir DIR] ...
team-board gate: error: the following arguments are required: --gate
rc=2        ← stderr 141 字节 / 2 行、stdout 0 字节
```

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
   **但摘要 ≠ R3 全套证据**：R3 要的是「rc0 / rc1 / rc2 各一条复跑命令」加声明过的取值域边界
   （0 / 负数 / 13 位毫秒 / 天文数字 / 空串各一条），gate 摘要只覆盖**它包装的那一条命令**。
   拿一条 `pytest` 的 `exit: 0` 当全套证据，照样会被打回 —— 缺的那几条得自己再跑、再贴；
   **`counts:` 是全文计数行累加出来的**，不是「最后一次跑的结果」：`gatelog.counts_line` 对整段
   输出 `findall` 后逐项相加（`totals[key] += int(raw)`），同名项在整份日志里出现几次就加几次。
   所以它只对「一个工具、一段输出」成立；多步命令（`sh -c 'pytest …; ruff …'`、一个脚本里连跑
   几个检查器）里两条 `12 passed` 会加成 `passed=24`，`error` / `skipped` 同理。
   **多步命令只认 `exit:`**：实测两条计数行（`3 passed` / `2 failed`）+ 末条 `exit 0` →
   `exit: 0`、`counts: passed=3 failed=2 error=0 skipped=0` ——「failed=2 却 exit 0」不是 gate
   判错，是它不重算、也只拿得到最后一条命令的 rc；要每步的判定值就分几次 gate 跑；
4. **摘要按行截断，不按字节**：失败行 / 末尾行各 ≤20 **行**，单行长度不设上限 —— 单行巨长
   （JSON dump、长 lint 行、`body-audit --all` 的超限清单单行）时摘要**自己**就超 8192，
   照第 1 条贴照样违规。实测一条 200000 字符的单行：摘要与日志**各约 200 KB**（摘要总共才 8 行）。
   **这两个数别照抄**：它们只是**当次实测**，随命令输出量与 `log:` 路径长度漂移 —— 同一条命令
   只把 `--log-dir` 换成更长的目录名，摘要字节数就等量跟着变（实测 `/tmp/a1` 200154 字节 vs
   `/tmp/a1234567890` 200163 字节，目录名长 9 字节、摘要也长 9 字节）。复跑以当场 `wc -c` 为准：
   先量再贴，摘要 `| wc -c` 超 8192 就只贴 `log:` 路径 + `sed -n '<a>,<b>p' <日志>` 取段；
5. 同一条边界也管**评论 / `tasks.result` 本身**：UTF-8 字节上限 `gatelog.OUTPUT_LIMIT_BYTES` = 8192，
   全文贴法一超就违规。这一条由 `output-audit` 子命令**机器判定**（已合入 `main`，实现在
   `src/team_board/outputaudit.py`）：rc0 = 无超限 / rc1 = 有超限 / rc2 = 参数或环境错误；
   默认只算未结卡，`--all` 连 `done` 卡一起算。

   自检命令写死「**在本卡 worktree** + `env PYTHONPATH=<本卡 worktree>/src`」（绝对路径，
   理由见 §1 前置第 4 条）：

   ```
   $ cd /home/liu/project/tb-wt/t_facc5c45 && env PYTHONPATH=/home/liu/project/tb-wt/t_facc5c45/src \
       /home/liu/project/team-board/.venv/bin/python -m team_board.cli output-audit --help
   ```

   rc0 才算树对了。**主 checkout 若还没 fetch 到 `ff679a7`**（output-audit 进 `main` 的那个提交），
   同一条命令 rc2 `team-board: error: argument command: invalid choice: 'output-audit'` ——
   那是**树的问题，不是有超限**（2026-10-31 实测：本卡 worktree rc0；主 checkout @ `21d40b9` rc2，
   而 `git merge-base --is-ancestor ff679a7 origin/main` rc0）。

## 5. 边界

- **不改变既有子命令的输出契约**：`body-audit` / `branch-audit` / `merge-audit` 等一行没动，
  gate 只是把它们包起来跑；判定与格式仍归它们自己。
- **接入旧工具的方式 = 包一层 gate**，不是改造工具本身：工具照旧裸跑可用，要落盘 + 摘要
  就在它前面加 `gate --gate <名> --card <卡号> --`。
- **gate 不判定通过与否**：`exit:` 是被包装命令的 rc 透传，`counts:` 是从它自己的输出里
  解析的值；gate 不重算、不解释，输出唯一的改写点是 §2 的 UTF-8 解码替换。
- 本文只描述契约，改动实现在 `src/team_board/cli.py`（`gate` 子命令）与
  `src/team_board/gatelog.py`（命名 / 落盘 / 摘要的唯一实现）。

## 6. 本机实跑记录

2026-10-31 01:17–01:20 CST 首轮实跑，当时 `gate` 只在实现卡 worktree
`/home/liu/project/tb-wt/t_eaa81611`（分支 `wt/t_eaa81611`）里；01:33 CST 用同一绝对路径解释器
复核全表。同日 PR #50 把 `gate` 合入 `main`（merge `21d40b9`）后又在主 checkout
`/home/liu/project/team-board` 重跑过 §1 的三个样例；返工轮（2026-10-31 11:00 CST 前后）按 §1
前置第 3 条改在**本卡 worktree** `/home/liu/project/tb-wt/t_facc5c45` 重跑，§1 三块摘要即那一轮原文。
下表数字是 **2026-10-31 CST 当日快照**，真板卡数会漂移。解释器为绝对路径
`/home/liu/project/team-board/.venv/bin/python`（3.14.7）—— `t_eaa81611` 根目录没有 `.venv`
（个别 worktree 有），所以外层 `gate` 与 `--` 之后的检查器用的是同一个绝对路径解释器：

| 场景 | 命令里的 `--gate` | rc |
| --- | --- | --- |
| 通过 | `pytest tests/unit/test_gatelog.py -q` | 0 |
| 通过 | `ruff check src tests` | 0 |
| 通过 | `mypy src` | 0 |
| 有历史存量 | `body-audit --all` | 1（`counts: 未解析`，`末尾行（20/292）`，142 张超限） |
| 失败透传 | `pytest` 一个必失败用例 | 1（`counts: passed=0 failed=1`） |
| 透传 2 | `/bin/sh -c 'echo hi; exit 2'` | 2（有摘要、无 stderr） |
| 被信号杀 | `/bin/sh -c 'kill -9 $$'` | 137（`128 + 9`，摘要里不带负数） |
| 起不来 | `nosuchcmd_xyz` | 2（stderr 恰一行、无日志） |
| argparse 缺必填参数 | 缺 `--gate` | 2（`usage:` + `error:` 两行、stdout 空、无日志） |
| 日志目录不可写 | `--log-dir /proc/nope/logs` | 2（stderr 恰一行） |
| stdout 编码非 UTF-8 | `PYTHONIOENCODING=ascii` | 2（stderr 恰一行，提示改用 UTF-8） |
| 卡号非法 | `--card t_XYZ` | 2（stderr 恰一行、无日志） |
| `--` 后为空 | `--` | 2（stderr 恰一行） |
| `--log-dir` 覆盖 | `--log-dir <tmp>/logs` | 0，日志落在覆盖目录、`evidence/` 未新建 |
