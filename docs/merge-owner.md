# PR 合并归属稽核（merge-owner-audit）

**一句话定位**：卡正文或评论里出现过 PR 号的卡，在关闭（`kanban_complete`）之前跑它 ——
`rc=0` 才许关；`rc=1` = 这个 PR 没有合并归属，先建一张收口卡（正文首行 `PR-CLAIM: #<n>`）
再关本卡。

口径唯一源是需求卡 `t_6fca5f99` 冻结的契约 v1，实现分两层：判定纯函数在
`src/team_board/mergeowner.py`（`t_b32c82d6`），本命令只做接线（读库、逐个 PR 发一次
`gh pr view`、降级留痕，`t_e12888d4`）。**判定没有第二份实现**。

## 用法

```
team-board merge-owner-audit <卡号> [--db PATH] [--repo OWNER/REPO] [--timeout N] [--log-dir DIR]
```

- `<卡号>`（位置参数，必填）—— `t_` + 8 位小写十六进制，例如 `t_0123abcd`；大写十六进制、
  少一位、多一位、带前后缀一律 rc2。
- `--db PATH` —— 看板 SQLite 路径，**只读**；默认
  `/home/liu/.hermes/kanban/boards/devteam/kanban.db`。
- `--repo OWNER/REPO` —— `gh` 查 PR 用的仓库；默认 `alsj213/team-board`。
- `--timeout N` —— 单次 `gh` 调用的超时秒数，取值 `1`–`600` 的 ASCII 十进制整数，默认 `20`。
  这一项**不挂 `type=int`**，所以 `0` / `-1` / `601` / 13 位毫秒 / 空串 / `20abc` 都是
  `错误: …` + rc2（不是 argparse 的用法报错）。
- `--log-dir DIR` —— 降级日志目录，默认 `evidence`（不存在自动建）。

## 输出

每行一条判定（PR 号升序），末行一句结论：

```
卡 t_cd645098 [done]
PR #46 MERGED
PR #47 MERGED
PR #49 MERGED
结论: 3 个 PR 全部有归属 → 可关
```

五种判定，**每个 PR 只出一个**，按下面优先级依次判（前者命中就不再看后者）：

| 优先级 | 判定 | 条件 |
| --- | --- | --- |
| 1 | `EXEMPT` | 本卡顶格豁免行 `PR-MERGE-EXEMPT: #<n>` 里含这个号 |
| 2 | `MERGED` / `CLOSED` | `gh pr view` 回 `MERGED` / `CLOSED` |
| 3 | `CLAIMED <卡号,…>` | PR 仍 `OPEN`，且**另一张**未结卡（`status NOT IN ('done','archived')`、且不是本卡）正文有 `PR-CLAIM: #<n>`；多张卡认领也照样通过，`detail` 是卡号升序逗号分隔 |
| 4 | `UNOWNED` | PR 仍 `OPEN`，且没有任何「另一张未结卡」认领 |
| 5 | `WARN <原因>` | `gh` 取数失败（rc≠0、输出非 JSON、状态不在 `MERGED`/`CLOSED`/`OPEN` 三态里） |

两条容易踩的细节：

- **本卡自己认领不算归属**：本卡正文写着自己的 `PR-CLAIM: #<n>` 但 PR 还开着，仍判
  `UNOWNED` —— 防「自认自关」。
- **抽取口径**：`PR #<n>`（`PR` 与 `#` 之间恰好一个空格）与 URL 的 `/pull/<n>` 两种；`PR #0`、
  10 位以上（含 13 位毫秒）不命中，不会把长数字截短收下。

## 退出码（三档，各一条本机真跑）

下面每条实测于 2026-10-31 11:57 CST，原始输出见附件 `t_20b036e1-samples.log`。跑的位置是
**E2 分支的 worktree `/home/liu/project/tb-wt/t_e12888d4`（PR #59 head `fffdcba`）** —— 本卡
PR 只改文档，本分支里没有该子命令的代码；`#59` 合并后同样的命令在主仓根目录直接可跑。
**看板状态会变，换张卡复跑口径不变，rc 可能变**。

`0` —— 没有 PR / 全部有归属（`MERGED`、`CLOSED`、`CLAIMED`、`EXEMPT`）/ 降级放行：

```
$ PYTHONPATH=src python3 -m team_board.cli merge-owner-audit t_dd98c031 | exit 0 | 结论: 抽取到 0 个 PR 号 → 无合并归属需要（直接通过）
$ PYTHONPATH=src python3 -m team_board.cli merge-owner-audit t_cd645098 | exit 0 | 结论: 3 个 PR 全部有归属 → 可关
```

`1` —— 有 `UNOWNED`；**即使同时有 `WARN` 也是 1**（UNOWNED 压过 WARN）：

```
$ PYTHONPATH=src python3 -m team_board.cli merge-owner-audit t_b32c82d6 | exit 1 | 结论: 6 个 PR / 通过 5 / 无归属 1 → 先建收口卡（首行 PR-CLAIM: #12）再关本卡
```

`2` —— 参数或环境错误：卡号形态非法、`--timeout` 越界、`--repo` 非法、卡不存在、库打不开 /
不是合法 SQLite。rc2 时 **stdout 一个字节都不写、`gh` 一次都不发**，stderr 恰一行以 `错误: `
开头，不冒 traceback：

```
$ PYTHONPATH=src python3 -m team_board.cli merge-owner-audit t_20B036E1 | exit 2 | 错误: 卡号非法：'t_20B036E1'（应为 t_ 加 8 位小写十六进制，例如 t_0123abcd）
$ PYTHONPATH=src python3 -m team_board.cli merge-owner-audit t_cd645098 --timeout 0 | exit 2 | 错误: 超时非法：'0'（应为 1-600 的整数秒）
```

## 豁免：`PR-MERGE-EXEMPT: #<n>`

证据 PR、明确决定不合并的 PR，在本卡正文**顶格**写一行：

```
PR-MERGE-EXEMPT: #123
```

- 顶格、整行、无尾随字符；缩进一格、行尾多一个空格、`#0`、`#034` 都不算（CRLF 的 `\r` 认）。
- 实现上豁免面是「正文 + 全部评论」拼接后按顶格行匹配，写正文最稳。
- **不许拿它绕真交付 PR** —— 验收时 pm 会看：真交付 PR 被豁免，等于把「有没有人合」这件事
  从闸门里摘掉，按不通过处理。拿不准就留着让它判 `UNOWNED`，然后建收口卡。

## 降级（`gh` 不可用）

`gh` 不可用（命令不存在 / 超时 / rc≠0 / 输出不是 JSON）时**不拦**：每个取不到的 PR 记一条
`WARN`，整块输出末行变成 `结论: gh 不可用（<原因>）→ 降级放行（日志已写），晨报须列`，
**rc 仍是 0**，同时：

- stderr 一行 `warn: gh 不可用（<原因>）→ 降级放行，晨报须列`；
- 每条 `WARN` 追加一行到 `<log-dir>/merge-owner-audit-<卡号>.log`，行格式
  `<UTC ISO8601 秒精度> warn pr=<n> <原因>`（实测三行 `2026-10-31T03:57:25Z warn pr=46 gh rc=1`）。
- 日志目录不可写只在 stderr 补一行 `warn: 日志写入失败: …`，**rc 不变**（日志是留痕，不是判定）。

实测（`gh` 换成固定 `exit 1` 的假命令）：

```
$ PATH=<假 gh 目录>:$PATH PYTHONPATH=src python3 -m team_board.cli merge-owner-audit t_cd645098 | exit 0 | 结论: gh 不可用（gh rc=1）→ 降级放行（日志已写），晨报须列
```

**降级必须进晨报**：一行写清「哪天 / 哪张卡 / 什么原因」。降级是「今天没查成」，不是「查过
没问题」—— 静默放行等于把闸门摘了。

## 与 `pr_orphan_autoclose.py` 的分工

两层都在，别互相替代：

- **本命令 = 源头拦截**（关闭前）：卡要关，先证明它交付的每个 PR 都有合并归属。没过就关不了。
- **`pr_orphan_autoclose.py` = 漏网修复**（事后）：PR 开着却没有任何未结卡认领 → 自动建一张
  收口卡（首行 `PR-CLAIM: #<n>`；按 R15 合并权归 repo，`assignee` 必须是 `repo`）交给 repo bot。
  当前由 ops 的 crontab 每 30 分钟跑一次
  （`*/30 * * * * … pr_orphan_autoclose.py --apply`），脚本默认 dry-run、`--apply` 才建卡，
  跳过 head 前缀 `evidence/` 与标题含「证据 PR / 不合并」的 PR，同一 PR 12 小时内只自动建一次、
  每轮最多 2 张。
- 两个都漏才会出现「PR 开着、卡 done、没人合」—— 所以本命令是**卡关之前**的最后一道，不是
  可有可无的提醒。
