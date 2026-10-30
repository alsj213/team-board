# push-guard：把 checkout 偏移从「事后稽核」提前到「push 之前」

`.githooks/pre-push` 只是**薄壳**：拿工作树根 → 判「这次到底有没有 ref 要传」（stdin
整块收进变量，空就直接放行）→ 把 ref 行喂给 `team-board push-guard` → 照它的退出码
放行 / 拦截，并把「判定红 / 环境错误 / 跑不起来」拆成三套文案。**唯一判定源是
`src/team_board/pushguard.py`**，shell 里不写第二份判断（也要这么改才不算跑偏）。
钩子**不落任何临时文件**：坏 `TMPDIR`、可预测文件名被预置符号链接、`SIGINT` 清理
三个问题一起消失（2026-10-30 返工 `t_a875c1ed` 定）。

```
$ git push …            # 钩子被 git 调用
  └─ .githooks/pre-push
       └─ PYTHONPATH=$root/src python3 -m team_board.cli push-guard --repo "$root"   ← 读 stdin 的 ref 行
            └─ pushguard.judge()  → P-1 / P-2 / P-3 / P-4
```

## 为什么要有它

私有仓免费计划下开不了分支保护（`gh api repos/<owner>/<repo>/branches/main/protection`
→ 403 `Upgrade to GitHub Pro`），护栏只能放客户端。规则 B（checkout 偏移）本身是
**事后**稽核（`branch-audit`，CI job `branch-audit (卡分支/checkout)`）；本钩子把同一
口径提前到推之前 —— **一个事前拦、一个事后稽核，口径同一份**。

## 判定表

顺序固定 **P-1 → P-2 → P-3 → P-4**，不短路（全命中就全报）。任一条 `blocked`
→ 整次 push 中止。

| 编号 | 触发条件 | 文案模板 | 与 branch-audit 的关系 |
| --- | --- | --- | --- |
| **P-1** | 当前 checkout **是主 checkout**（`--git-common-dir` 的父目录）且 HEAD 不是 `<main-branch>`（detached 也算） | `拒绝：主 checkout <root> 停在 <actual>，必须停 main` | 规则 B「主 checkout 必须停在 `<main-branch>`」的同一条 |
| **P-2** | 主 checkout 上推/收**卡分支** `wt/t_xxxxxxxx`（本侧或远端侧任一命中） | `拒绝：主 checkout 不许推送卡分支 wt/<卡号>；做法：git -C <root> worktree add /home/liu/project/tb-wt/<卡号> -b wt/<卡号> origin/main` | 规则 B「卡分支必须在 `t_<卡号>` 的 worktree 里」的前置 |
| **P-3a** | 非主 checkout、HEAD 是卡分支、**目录名 ≠ 卡号** | `拒绝：worktree <root> 的目录名 <name> 与所推卡分支 wt/<卡号> 不一致（应为 t_<卡号>）` | 规则 B「分支是 `wt/t_<X>` 时目录名必须精确是 `t_<X>`」的同一条 |
| **P-3b** | 非主 checkout、HEAD detached | `拒绝：worktree <root> 处于 detached@<sha7>，无法归属到卡分支` | 规则 B「其它 worktree 的 detached HEAD 一律判红」 |
| **P-3c** | 非主 checkout、HEAD 是卡分支 X，却推别的卡分支 Y | `拒绝：worktree 上的卡 <X> 不许推送别的卡分支 wt/<Y>` | 规则 C-1「一卡一分支」的前置 |
| **P-4** | `remote_ref` 是 `refs/heads/main` / `refs/heads/master`（含删远端分支的全零 sha） | `拒绝：不允许直接 push 到 <main\|master>`（删除追加一行 `（这是删除远端分支的操作，同样禁止）`） | 无（CI 侧没有对应稽核；这是原有保护，本卡只保证不丢） |

没有命中任何一条时的放行文案（stdout 恰一行）：

```
push-guard: 放行 N 条 ref（checkout=<root>，HEAD=<actual>）
```

## 退出码

`push-guard` 子命令（`team-board push-guard`）：

- `0` —— 放行（stdout 恰一行；`--check-install` 时表示钩子已装好）；
- `1` —— 拦下（stdout 空，stderr 每处一行以 `拒绝：` 开头）；
- `2` —— 参数或环境错误（`--repo` 不存在 / 不是工作树、`--main-branch` 为空或该
  分支不存在、stdin 每行字段数 ≠ 4、stdin 为空、编码失败）；stderr 一行 `错误: …`。

钩子层（`git push` 看到的那一层）—— **只有 `0` / `1` 两个码**，而且这条是**设计保证**
（钩子不落临时文件、自己判 stdin），不是「按理说只有」：

**例外（一个）**：**调用方把钩子的 fd1 关掉**（例如 `git push >&-`）时钩子拿不到可写的
stdout，`exec 3>&1` 直接失败 —— 实测（2026-10-30）dash **rc=2**（只吐一句 `1: Bad file
descriptor` 即退，没有任何人话）、bash **rc=1**（`3: Bad file descriptor`
后继续跑到判定红那档，还打了两行提示）。这是调用方用法的问题，不是钩子自己的判定：
正常 `git push` 一定有 stdout，`0` / `1` 的保证只在这个前提下成立。

| 情形 | push-guard rc | 钩子 rc | 钩子说什么 |
| --- | --- | --- | --- |
| 放行 | 0 | 0 | 原样回放 push-guard 的 stdout |
| **空 stdin** —— `git push` 在「已是最新」（没有要更新的 ref）时仍会调用钩子，但一行都不喂 | 不调用 | 0 | **不打印**。没有 ref 会被传输，也就没有可拦的东西。判据由钩子自己算（stdin 整块收进变量后为空），不依赖 push-guard 的文案、不 grep 临时文件 |
| 判定红 | 1 | 1 | 回放 push-guard 的 `拒绝：…` 行 + 下面两行 `[pre-push] …` 提示 |
| 环境错误（编码失败、`--repo` 不是工作树、主分支 ref 不存在…） | 2 | 1 | `[pre-push] 拒绝：push-guard 判定不了 —— 环境错误（rc=2），不是你的代码有问题（fail-closed）` + 常见原因 + push-guard 的原始诊断；**不打** `正确做法` / `免检通道` 两行（误火时给绕过口子是错的） |
| 版本偏斜（本 checkout 的 `src` 里还没有 `push-guard` 子命令，argparse `invalid choice`） | 2 | 1 | `[pre-push] 拒绝：本 checkout 的 src 里还没有 push-guard 子命令（版本偏斜）…`；**不**把 argparse usage 泄给用户 |
| **fail-closed**（钩子自己判，不经 push-guard） | — | 1 | `拒绝：拿不到工作树根（fail-closed）` / `拒绝：push-guard 跑不起来（fail-closed，见 .githooks/README.md）`（`python3` 不在 `PATH`、缺 `src/team_board/cli.py`、解释器/导入级崩溃都归这条） |

判定红时钩子另补两行提示（**只有 rc=1 判定红才补**；环境错误与 fail-closed 不补）：

```
[pre-push] 正确做法：git -C <主 checkout> worktree add /home/liu/project/tb-wt/<卡号> -b wt/<卡号> origin/main
[pre-push] 免检通道：git push --no-verify 能绕过本钩子，但 CI 的 branch-audit (卡分支/checkout) 仍会判红
```

worktree 落点统一写 `/home/liu/project/tb-wt/`（本机现存约定：**多数** worktree 落在这个
前缀下；准确条数现数现取，别写死在文档里 ——
`git worktree list | awk '{print $1}' | grep -c '^/home/liu/project/tb-wt/'`），
push-guard 的 P-2 文案与钩子提示用的是同一个前缀。

## 怎么装、怎么自查

`core.hooksPath` 不进版本库，每个 clone 首次都要手动指一次：

```
git config core.hooksPath .githooks
```

**「装一次」只解决「git 会不会调用」，不解决「调用的是哪一版钩子」**：`core.hooksPath`
只记路径，钩子**内容跟着各自 checkout 的分支走**。合并后没 rebase 的 worktree 里
`.githooks/pre-push` 还是旧版本（2026-10-30 实测：26 个 worktree 里 1 个新、25 个旧），
P-1…P-4 与新文案不会因为主 checkout 指过一次就全生效 —— 要用新钩子，把那个 worktree
`git rebase origin/main` 一下。

装完自查：

```
team-board push-guard --repo . --check-install
```

**这条 rc=0 只证明「`core.hooksPath` 指对了 + 钩子文件在 + 可执行」，它不看钩子
内容**（历史已知假绿：钩子还是纯 shell 拦 main/master 时它也 rc=0）。要证明
P-1…P-4 真生效，看下面「证据形态」里的真 push 红样例，或一条内容断言：

```
grep -q 'team_board.cli push-guard' .githooks/pre-push
```

## 「拦在 push 之前」的证据形态

只看 git 的报错（`error: failed to push some refs`）不够 —— 那也可能发生在
「推完了才报」。唯一硬的证据是**远端实际状态**：把远端换成一次性裸仓
（`git init --bare`），push 前后各看一眼：

```
$ git -C <bare> for-each-ref          # push 之前
$ git push <bare> <卡分支>            # rc=1，stderr 出现「拒绝：…」
$ git -C <bare> for-each-ref          # push 之后：仍然为空 = 一个字节都没传过去
```

`tests/integration/test_pre_push_hook.py` 的用例 a 就是这么断言的（`refs(remote) == ()`）。

**不要拿 origin 做红样例**：万一没拦住就污染了真仓库。所有红样例一律用
`mktemp` 下的裸仓当远端。

## 剩余风险（5 条，逐条不含糊）

1. **`git push --no-verify` 绕过所有客户端钩子** —— 本卡不保证 100% 拦截。权威判定
   仍是 CI job `branch-audit (卡分支/checkout)`；钩子只是让越界在本地当场疼一下。
   （`--no-verify` 会让 git 直接跳过本钩子，机器层面无从拦截，所以也不写用例。）
2. **新 clone / 容器里没配 `core.hooksPath`** —— 钩子文件在，但 git 根本不调用。
   `push-guard --check-install` 能查出来，但**不强制生效**；进容器/新机器要手动跑一次
   `git config core.hooksPath .githooks`。
3. **不经本机 `git` 的推送** —— GitHub 网页编辑、另一台机器、`gh api` 手搓提交、
   Git Data API 全都不跑本机钩子，覆盖不到。
4. **直推 `main` 没有平台级强制点** —— `gh api repos/alsj213/team-board/branches/main/protection`
   → 403（私有仓免费计划），只能靠钩子 + CI 事后稽核（`git log --first-parent` 视检）。
5. **本卡不动 `.github/`**（避免升 R7 C 档）—— 因此**不新增 CI job**；P-1…P-4 的
   CI 侧口径仍只有现成的 `branch-audit` job。

## 实测（返工 `t_a875c1ed`，2026-10-30）

钩子直接喂 ref 行（不碰 origin、不切主 checkout 的分支）。

红（临时仓当主 checkout、HEAD 在 `main`，推卡分支）：

```
$ printf 'refs/heads/wt/t_06c7b458 <sha> refs/heads/wt/t_06c7b458 <全零 sha>\n' | /bin/sh .githooks/pre-push
拒绝：主 checkout 不许推送卡分支 wt/t_06c7b458；做法：git -C /tmp/pg-probe.EAWFZQ/repo worktree add /home/liu/project/tb-wt/t_06c7b458 -b wt/t_06c7b458 origin/main
[pre-push] 正确做法：git -C <主 checkout> worktree add /home/liu/project/tb-wt/<卡号> -b wt/<卡号> origin/main
[pre-push] 免检通道：git push --no-verify 能绕过本钩子，但 CI 的 branch-audit (卡分支/checkout) 仍会判红
rc=1
```

绿（同一临时仓推普通分支 `feat-x`）：

```
$ printf 'refs/heads/feat-x <sha> refs/heads/feat-x <全零 sha>\n' | /bin/sh .githooks/pre-push
push-guard: 放行 1 条 ref（checkout=/tmp/pg-probe.EAWFZQ/repo，HEAD=main）
rc=0
```

环境层三例（改前 = `f1c8b44` 的钩子，改后 = 本次；都是钩子直接喂 stdin）：

| 场景 | 改前 | 改后 |
| --- | --- | --- |
| 空 stdin × `TMPDIR=/nonexistent-xyz` × `/bin/sh`(dash) | `rc=2`，`cannot create /nonexistent-xyz/pre-push-*.err: Directory nonexistent`，**一句 `[pre-push]` 人话都没有** | `rc=0`，无输出（钩子不落临时文件） |
| 空 stdin × `TMPDIR=/nonexistent-xyz` × `bash` | `rc=1`：零传输的 push 被误拦（`errfile` 退化 `/dev/null` 后 grep 判据失效） | `rc=0`，无输出 |
| `PYTHONIOENCODING=ascii` × 一条**该放行**的 ref | `rc=1` + `正确做法` / `--no-verify` 两行（把 push-guard 的 rc2 当判定红） | `rc=1` + 「环境错误（rc=2）」档 + 原始诊断，**无** `--no-verify` |

改前的三条 rc 是拿 `git show f1c8b44:.githooks/pre-push` 落成临时文件实跑的，不是转述；
四条新断言钉在 `tests/integration/test_pre_push_hook.py` 用例 i–l。

回归（规则 B 的事后稽核没被改坏，新旧代码同一时刻跑真仓，输出 diff 为空）：

```
$ PYTHONPATH=<origin/main 的 src> python -m team_board.cli branch-audit --repo /home/liu/project/team-board   # rc=1
$ PYTHONPATH=<本卡 src>            python -m team_board.cli branch-audit --repo /home/liu/project/team-board   # rc=1
$ diff <(旧) <(新)     # stdout / stderr 均无差异
```
