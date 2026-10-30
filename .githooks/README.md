# .githooks

`core.hooksPath` 不进版本库，每个 clone 首次都要手动指一次：

    git config core.hooksPath .githooks

不配则该 clone 的 pre-push 护栏不生效（钩子文件在，但 git 不会调用）。

**「装一次」只解决「git 会不会调用」，不解决「调用的是哪一版钩子」**：`core.hooksPath`
只记一个路径，钩子**内容跟着各自 checkout 的分支走**。合并后没 rebase 的 worktree 里
`.githooks/pre-push` 仍是旧版本（2026-10-30 实测：26 个 worktree 里 1 个新、25 个旧），
新规则不会因为「主 checkout 指过一次」就全生效 —— 要用新钩子，先 `git rebase
origin/main` 那个 worktree。

## pre-push：薄壳，判定在 `push-guard`

`.githooks/pre-push` 只做三件事：拿工作树根 → 判「这次到底有没有 ref 要传」（stdin
整块收进变量，空就直接放行）→ 把 ref 行喂给 `team-board push-guard`，照它的退出码
放行 / 拦截。主 checkout 是否停在 `main`（P-1）、主 checkout 是否在推卡分支（P-2）、
worktree 与卡分支是否一致（P-3）、`main` / `master` 保护（P-4）四条判定**全在
`src/team_board/pushguard.py`**，shell 里没有第二份判断。完整判定表与文案模板见
`docs/push-guard.md`，用户视角见 README「独立 worktree 与 push 前的拦截」一节。

**钩子不落任何临时文件**（2026-10-30 返工 `t_a875c1ed` 定）：坏 `TMPDIR`、可预测
文件名被预置符号链接、`SIGINT` 清理三个问题一起消失。**「有没有 ref」也由钩子自己
判**：不 grep 临时文件、不认 push-guard 的文案。

装完自查（`core.hooksPath` 指对了 + 钩子文件在 + 可执行）：

    team-board push-guard --repo . --check-install

**它不看钩子内容**：钩子还是纯 shell 拦 main/master 时这条也 rc0（已知假绿）。
要证明「钩子确实在调判定本体」，另配一条内容断言：

    grep -q 'team_board.cli push-guard' .githooks/pre-push

## 三档文案：钩子层退出码只有 0 / 1

钩子把「判定红」和「环境错误」分开说 —— 误火时把 `--no-verify` 这种绕过口子递给
用户是错的。**0 = 放行，1 = 拦（含环境错误 fail-closed），没有第三个码**。

| 情形 | push-guard rc | 钩子 rc | 钩子说什么 |
| --- | --- | --- | --- |
| 放行 | 0 | 0 | 原样回放 stdout：`push-guard: 放行 N 条 ref（…）` |
| 空 stdin（没有 ref 要传） | 不调用（钩子自己判） | 0 | 什么都不打印 |
| 判定红 | 1 | 1 | 回放 push-guard 的 `拒绝：…` 行 + `正确做法` + `免检通道` 两行 |
| 环境错误 | 2 | 1 | `[pre-push] 拒绝：push-guard 判定不了 —— 环境错误（rc=2）…` + 常见原因 + 原始诊断；**不**打 worktree add / `--no-verify` |
| 版本偏斜（缺 `push-guard` 子命令） | 2（argparse `invalid choice`） | 1 | `[pre-push] 拒绝：…还没有 push-guard 子命令（版本偏斜）…`；**不**把 argparse usage 泄给用户 |
| fail-closed | — | 1 | `[pre-push] 拒绝：…（fail-closed…）` |

## fail-closed：跑不起来就拒绝，不静默放行

钩子自己判的三种「跑不起来」都 `exit 1` 并打印人话：

- `git rev-parse --show-toplevel` 拿不到工作树根 → `拒绝：拿不到工作树根（fail-closed）`；
- `python3` 不在 `PATH`，或本 checkout 缺 `src/team_board/cli.py` →
  `拒绝：push-guard 跑不起来（fail-closed，见 .githooks/README.md）`；
- push-guard 解释器 / 导入级崩溃（stderr 出现 `Traceback (most recent call last)`）→
  同上那句，不再给 `--no-verify` 提示。

**宁可推不上去，也不静默放行。**

修法：确认本仓 `src/` 在（钩子把 `$root/src` 塞进 `PYTHONPATH`）、`python3` 可用
（本仓纯标准库，任意 3.11+ 即可）、`git config core.hooksPath` 指向本目录。

## 绕过方式（已知剩余风险，不是后门）

`git push --no-verify` 会让 git 直接跳过**所有**客户端钩子，机器层面无从拦截；
新 clone / 容器里没配 `core.hooksPath` 时钩子根本不跑。所以本钩子只是「推之前
当场拦住」，权威判定仍是 CI job `branch-audit (卡分支/checkout)`。
