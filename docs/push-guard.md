# push-guard：把 checkout 偏移从「事后稽核」提前到「push 之前」

`.githooks/pre-push` 只是**薄壳**：拿工作树根 → 把 git 喂进来的 ref 行原样透传给
`team-board push-guard` → 照它的退出码放行 / 拦截。**唯一判定源是
`src/team_board/pushguard.py`**，shell 里不写第二份判断（也要这么改才不算跑偏）。

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
| **P-2** | 主 checkout 上推/收**卡分支** `wt/t_xxxxxxxx`（本侧或远端侧任一命中） | `拒绝：主 checkout 不许推送卡分支 wt/<卡号>；做法：git -C <root> worktree add /home/liu/tb-wt/<卡号> -b wt/<卡号> origin/main` | 规则 B「卡分支必须在 `t_<卡号>` 的 worktree 里」的前置 |
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

钩子层（`git push` 看到的那一层）：

- `0` —— 放行（原样回放 push-guard 的 stdout / stderr）；也包括「**空 stdin**」：
  `git push` 在「已是最新」（没有要更新的 ref）时仍会调用钩子，但一行都不喂，
  push-guard 判 rc2 `没有收到任何要推送的 ref（stdin 为空）` —— 没有任何 ref 会被
  传输，也就没有可拦的东西，钩子放行且**不打印**任何东西。判据是 push-guard 那句
  固定文案（rc2 + 该串），不在 shell 里另算一遍「有没有 ref」；
- `1` —— 拦截。三种来源都算：push-guard 自己判红（rc1）、push-guard 环境错误
  （除空 stdin 外的 rc2，**仍拦**，不静默放行）、以及 **fail-closed**（见下）；
- **fail-closed 两种情况**（钩子自己判，不经 push-guard）：`git rev-parse
  --show-toplevel` 拿不到工作树根 → `[pre-push] 拒绝：拿不到工作树根（fail-closed）`；
  `python3` 找不到 / 模块导入失败（rc 126/127 或 stderr 出现 `No module named`）→
  `[pre-push] 拒绝：push-guard 跑不起来（fail-closed，见 .githooks/README.md）`。
  两种都 `exit 1` —— **宁可推不上去，也不静默放行**。

拦截时钩子另补两行提示（无论哪种拦截理由）：

```
[pre-push] 正确做法：git -C <主 checkout> worktree add /home/liu/tb-wt/<卡号> -b wt/<卡号> origin/main
[pre-push] 免检通道：git push --no-verify 能绕过本钩子，但 CI 的 branch-audit (卡分支/checkout) 仍会判红
```

## 怎么装、怎么自查

`core.hooksPath` 不进版本库，每个 clone 首次都要手动指一次（worktree 共享同一份
`.git/config`，所以指一次主 checkout 就够）：

```
git config core.hooksPath .githooks
```

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

## 实测（本卡，2026-10-30）

红（主 checkout 停在非 `main` 分支 + 推现存卡分支，远端为一次性裸仓）：

```
$ git -C /home/liu/project/team-board -c core.hooksPath=/home/liu/tb-wt/t_27bc948e/.githooks push <临时裸仓> wt/t_06c7b458
拒绝：主 checkout /home/liu/project/team-board 停在 tmp/pg-red，必须停 main
拒绝：主 checkout 不许推送卡分支 wt/t_06c7b458；做法：git -C /home/liu/project/team-board worktree add /home/liu/tb-wt/t_06c7b458 -b wt/t_06c7b458 origin/main
rc=1        # 裸仓 for-each-ref 为空：拦在传输之前
```

绿（独立 worktree `/home/liu/tb-wt/t_27bc948e`，目录名 = 卡号、HEAD = `wt/t_27bc948e`）：

```
$ git push <临时裸仓> wt/t_27bc948e
push-guard: 放行 1 条 ref（checkout=/home/liu/tb-wt/t_27bc948e，HEAD=wt/t_27bc948e）
rc=0        # 裸仓出现 refs/heads/wt/t_27bc948e
```

回归（规则 B 的事后稽核没被改坏，新旧代码同一时刻跑真仓，输出 diff 为空）：

```
$ PYTHONPATH=<origin/main 的 src> python -m team_board.cli branch-audit --repo /home/liu/project/team-board   # rc=1
$ PYTHONPATH=<本卡 src>            python -m team_board.cli branch-audit --repo /home/liu/project/team-board   # rc=1
$ diff <(旧) <(新)     # stdout / stderr 均无差异
```
